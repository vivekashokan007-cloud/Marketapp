# Protocol draft — does a historical premium range add value over an IV-only range?

**Status** DRAFT, not frozen. 17 November 2026 IST. ·
**Runner** `tools/premium_range_compare/range_compare.py`,
`premium_neutral_range_comparison_v1` · **Pricing** the frozen R7 contract `2.6`
(`tools/spot_path_baseline/premium_repricing_v2.py`, unmodified) ·
**Branch** `review/premium-neutral-range-comparison-20261116` from
`124d62ee027941e43decc737e53d202c32b1d214`

This draft is written to be **frozen before the first scored fresh export**, and everything in
it is stated before any fresh session has been seen. Where a number here comes from 12 November
it is labelled **development evidence** and is explicitly not part of the trial sample.

---

## 1 · The question, and the one thing it is not

**Primary question.** At the 60-minute horizon, does a range built from prior sessions' moves at
the same time of day score better than a range built from the contract's own entry implied
volatility, when both are centred on the same observable entry price?

**It is not a question about the centre.** The centre is the entry bid/ask midpoint and every
variant uses it. This matters because the comparison was previously confounded: the 12 November
120-minute NIFTY case had the IV-only range scoring worse than the frozen model on CRPS while
its nominal 80% interval covered 41% of outcomes, and it was not knowable from those two numbers
whether the range was the wrong *shape* or merely in the wrong *place*. Separating them is the
whole point of the common centre, and **MAE is reported to confirm the separation worked**: with
one centre, the flat point and both ranges must tie on MAE. If they do not, the centring is
broken and the CRPS comparison is not about shape. The runner asserts this tie on every real row
at every horizon.

## 2 · The variants

| variant | centre | dispersion from | recentred | role |
| --- | --- | --- | --- | --- |
| `FLAT` | entry midpoint | nothing | n/a | the floor a range must beat |
| `IV_ONLY` | entry midpoint | the contract's own entry IV, fitted from its midpoint under the parity forward, on a fixed 201-point quantile grid | yes | candidate |
| `HIST` | entry midpoint | prior sessions' log returns over the same clock interval, **median removed** | yes | candidate |
| `R7_E0_FWD` | not recentred | prior sessions' raw returns at the **quoted** IV | no | reference against the unchanged frozen replay |
| `IV_ONLY_RAW`, `HIST_RAW` | not recentred | as above | no | diagnostics: what the recentring actually changed |

Both candidates are repriced with the **same** fitted entry IV, the same clock and the same
bounds, so the only difference between them is the scenario distribution. That is deliberate: a
comparison in which the two models also disagreed about volatility or about the clock would not
answer the question asked.

**The median historical return is removed** from `HIST`. Without that, a sample with a drift
would appear to beat `IV_ONLY` by relocating the distribution — a different and much weaker
claim than adding value through shape.

## 3 · What is computable at entry, and nothing else

* **Forward** — put-call parity on matched call/put midpoints in the **same expiry, strike and
  acquisition** (the frozen `implied_forwards`): at least 2 strikes per `(underlying, expiry)`
  group, maximum cross-strike dispersion 25 points, median of the per-strike estimates. A
  contract may use only the forward of **its own entry acquisition**.
* **Entry IV** — fitted by bisection so that the frozen pricer reproduces the contract's own
  midpoint under that forward on the ACT/365 clock. The **quoted** IV is recorded beside it and
  never substituted for a failed fit. Three named failures, never clamped to a bracket endpoint:
  `iv_target_below_floor`, `iv_target_above_ceiling`, `iv_no_convergence`.
* **Quote bounds** — a midpoint at or below intrinsic, or at or above the price at the bracket
  ceiling, is **unsolvable**, not a number.
* **Clock** — one ACT/365 calendar term, expiry at 15:30 IST on each contract's own expiry date,
  entry at the entry acquisition's receipt, target at entry receipt + horizon. The scenario's own
  variance horizon is the hold, `T_entry - T_target` years.
* **Exit** — the exact same contract, the quote whose cutoff is nearest the **planned** target,
  admitted only within ±5 s. Identical across every variant and horizon.
* **Event time** — the export carries the app's acquisition receipt only. Exchange quote event
  time is **unverified**, and that bounds every number here: a receipt says when a price could be
  known, never how old it was.

### 3.1 · The broker-convention question is open, and the current evidence does not show a mismatch

On 12 November, with the parity forward and the ACT/365 clock, the **quoted** IV and the IV
**fitted** from the midpoint agree closely: median difference **+0.01** vol points (NIFTY, 246
rows; range −0.64 to +0.90) and **−0.01** (BANKNIFTY, 251 rows; range −0.47 to +0.56). Zero fit
failures on either index.

That is worth stating plainly because the handoff records the opposite ("the current evidence
shows a model-to-quote mismatch"). On this export there is no material mismatch at entry. The
most likely explanation for earlier mismatch evidence is the underlying reference rather than the
convention: R7's own 2.0 correction found index spot with `r = 0` gave entry basis errors of 8.27
(NIFTY) and 58.67 (BANKNIFTY) points, **antisymmetric in calls and puts** — a wrong underlying,
not a wrong volatility — and replacing spot with the parity forward removed it. **The convention
remains an open question**; what is settled is that it is not visible in these rows.

## 4 · The coverage ledger

Built from **intended opportunities**, defined before any later price is read: one per (pinned
contract, nominal capture minute) entry candidate, from the capture schedule and the pinned
universe.

```
intended opportunities
  → matched exit within ±5 s                 else rej_no_exit_quote_within_tolerance
  → target before expiry                     else rej_target_at_or_after_expiry
  → parity forward in the entry acquisition  else rej_no_forward_in_entry_acquisition_group
  → fitted entry IV                          else rej_iv_target_below_floor / _above_ceiling
  → ≥ 5 prior sessions with both endpoints   else rej_insufficient_prior_sessions   (HIST only)
  → common scored rows
```

**A row with no exit quote has no computable CRPS and still counts in the intended
denominator.** A session that stopped capturing must not read like a session that scored.

**Minimum usable coverage, proposed.** A session enters the trial sample only if, per index and
at the primary horizon, `common_scored ≥ 0.60 × intended_opportunities`. On 12 November that
ratio is 246/318 = **77%** (NIFTY) and 251/323 = **78%** (BANKNIFTY). A session below the
threshold is **reported and excluded**, with its ratio and its leading refusal reason; it is
never partially counted. An incomplete session is not a failed model.

**Every variant's own row count is reported beside the common set**, and the headline comparison
uses only rows where `FLAT`, `IV_ONLY` and `HIST` all scored. A model that fails on hard rows
cannot be credited with a better mean over the easy ones that remain.

## 5 · The historical join

| what | rule |
| --- | --- |
| slot | `--slot-policy exact` (default) reads the observation's own minute, which is what the frozen replay does. `floor5` floors to a five-minute slot, as a reproducible sensitivity, **labelled inferred in the manifest**. `explicit` reads the source's own scheduled-slot field and **fails closed** when the source has none. |
| window | the **rebuilt prior window is fixed** for the first controlled comparison: the pinned 855-row file, SHA-256 `f6f29ed8…`, 11 sessions 21 Oct – 4 Nov 2026. A rolling-history variant is listed separately and requires its timestamped inputs and update rule to be specified first. |
| endpoints | a delayed, duplicate or missing endpoint is **counted, never interpolated**: `hist_missing_entry_endpoint`, `hist_missing_exit_endpoint`, `hist_non_positive_price`, `hist_non_finite_return`, `hist_pairs_used`. |
| minimum | 5 usable prior pairs, the frozen `MIN_TRAIN`. |

### 5.1 · A discrepancy to resolve before freezing

The handoff states the review package has **explicit scheduled-slot fields on 233 of 855**
historical rows. The pinned file whose SHA-256 matches the handoff's own
(`f6f29ed8fd6e1290f3d0f59e9be2cdd36f236b945409aebb2a73edffd499d186`, 855 rows) carries **no
scheduled-slot field on any row**. Its fields are `d`, `t`, `nf`, `bnf`, `app_brain_version`, and
`rec_nf_dir` / `rec_bnf_dir` — recorded *directions*, non-null on **234** rows, all from
2026-11-02 to 11-04, with sampled value `UNAVAILABLE`. A recorded direction is not a scheduled
slot, and 234 is not 233.

So either the claim refers to a different file in the review package, or it is a misreading of
the 234 direction rows. **Until it is resolved, every historical join in this protocol reads an
inferred slot and says so.** The `explicit` policy exists, is tested, and refuses this source by
name rather than quietly inferring.

What *is* measurable on this file: **41 of 855** observation minutes are off the five-minute grid
(clocks such as 09:36, 10:01, 15:31), spread over 9 of the 11 sessions. Flooring them would
collide with an existing on-grid observation **0 times**, so on this sample `floor5` loses no
observation — which is why it is usable as a sensitivity. It still produces an inferred slot.

## 6 · Scoring, and the grid the gate depends on

CRPS for the range; nominal **80%** coverage and mean interval width reported **together**; MAE
to confirm the central tie. All four come from the frozen `R7.score`, so the scorer is not a new
variable.

**The quantile grid is pinned at 201 points, and the reason is a measured one.** Mean CRPS is
insensitive to the grid — NIFTY at 120 minutes moves from 31.732 at N=21 to 31.686 at N=401, a
0.15% spread — but nominal 80% coverage is not:

| grid N | 21 | 31 | 41 | 51 | 101 | 201 | 401 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `IV_ONLY_RAW` cov80 | 35.6% | 37.4% | 38.5% | 39.1% | **41.4%** | 42.0% | 42.0% |
| `IV_ONLY_RAW` CRPS | 31.732 | 31.700 | 31.696 | 31.690 | 31.688 | 31.687 | 31.686 |

A six-point swing is wider than the distance between a passing and a failing calibration gate, so
**a protocol that gates on coverage must pin the grid or the gate decides on an implementation
detail.** 201 is the smallest grid at which coverage is converged and CRPS is converged to four
significant figures. Mean `width80` still creeps upward with N — the 10th and 90th percentiles of
a discrete grid approach the continuous quantiles from inside — so width is reported **with the
grid pinned** and never compared across grids. `HIST` is grid-independent by construction: its
scenarios are the historical sample, and every `HIST` number is identical at every N (asserted in
the tests).

This also reconciles the handoff's own figure: **41.4% is exactly the 101-point grid**, and the
companion CRPS 31.69 is the un-recentred IV-only range at any grid from 41 up. Both reproduce.

## 7 · The decision rule

**Unit of observation: the session mean, per index.** Rows inside a session are not independent —
neighbouring strikes and overlapping windows see the same few underlying moves — so a 300-row
session must not outvote a 30-row one. Rows are averaged within a session per index, and sessions
are averaged across. Neighbouring strikes and overlapping windows are reported as observations
**within** the same session, never as independent evidence.

**Proposed settings** (from the handoff, carried here as proposals, not as settled values):

| setting | proposed | note |
| --- | --- | --- |
| practical improvement | **≥ 5%** mean CRPS improvement of `HIST` over `IV_ONLY`, at 60 minutes, on common rows | paired per session |
| calibration gate | nominal 80% coverage **between 70% and 90%** | applied to the variant being adopted |
| looks | 10, 15, 20 sessions | per index |
| minimum sessions | 10 | per index |
| minimum coverage | 60% of intended, per session per index | §4 |

**Repeated looks.** Three looks at a 5% threshold is three chances to cross it by noise, so the
rule has to say what a look may conclude:

* a look may **stop for adoption** only if the threshold is met **and** the calibration gate
  passes **and** the paired sign count agrees — `HIST` better in at least 7 of 10, 11 of 15, or
  14 of 20 sessions (the one-sided sign test at roughly 5% under a fair coin);
* a look may **stop for futility** if the point estimate is on the wrong side of zero at 15
  sessions;
* otherwise it **continues**. Not crossing is not evidence of equivalence.
* **The decision state and every reported number are independent of row order**, which the runner
  asserts directly.

**Coverage must be reported by time to expiry and by horizon**, not only pooled: a range model's
calibration is not one number, and the runner carries `days_to_expiry` on every row for exactly
this breakdown.

### 7.1 · The conflict the development evidence already shows — resolve this before freezing

On 12 November at 60 minutes, the two criteria point in **opposite directions**:

| index | `IV_ONLY` CRPS | `HIST` CRPS | `IV_ONLY` cov80 | `HIST` cov80 | `IV_ONLY` width80 | `HIST` width80 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| NIFTY (246 rows) | **16.840** | 18.096 | **54.9%** ✗ | 81.7% ✓ | 56.9 | 92.8 |
| BANKNIFTY (251 rows) | **34.456** | 38.046 | 78.5% ✓ | 87.6% ✓ | 151.9 | 213.4 |

`IV_ONLY` wins CRPS on both indices by 7–10%, and on NIFTY it **fails** the proposed 70–90%
calibration gate at 54.9% while `HIST` passes at 81.7%.

**And it is not a 60-minute quirk.** Across all four horizons and both indices, 12 November:

| index | horizon | rows | `IV_ONLY` CRPS | cov80 | gate | `HIST` CRPS | cov80 | gate | `HIST` vs `IV_ONLY` |
| --- | ---: | ---: | ---: | ---: | :---: | ---: | ---: | :---: | ---: |
| NIFTY | 15 | 300 | **8.219** | 64.7% | ✗ | 9.279 | 71.3% | ✓ | −12.90% |
| NIFTY | 30 | 282 | **11.034** | 67.4% | ✗ | 13.098 | 76.2% | ✓ | −18.70% |
| NIFTY | **60** | 246 | **16.840** | 54.9% | ✗ | 18.096 | 81.7% | ✓ | −7.46% |
| NIFTY | 120 | 174 | 31.857 | 41.4% | ✗ | **30.914** | 75.9% | ✓ | +2.96% |
| BANKNIFTY | 15 | 305 | **21.862** | 70.2% | ✓ | 23.007 | 78.4% | ✓ | −5.24% |
| BANKNIFTY | 30 | 287 | **27.823** | 71.1% | ✓ | 31.511 | 79.1% | ✓ | −13.25% |
| BANKNIFTY | **60** | 251 | **34.456** | 78.5% | ✓ | 38.046 | 87.6% | ✓ | −10.42% |
| BANKNIFTY | 120 | 179 | **36.890** | 92.2% | ✗ | 46.736 | 92.7% | ✗ | −26.69% |

`IV_ONLY` has the lower CRPS in **7 of 8** cells; `HIST` passes the calibration gate in **7 of 8**
while `IV_ONLY` passes in **3 of 8**. The two criteria therefore disagree almost everywhere, in
the same direction, which makes this a structural property of the two models rather than a
sampling accident of one horizon:

* NIFTY `IV_ONLY` is **systematically under-dispersed, and worse the longer the horizon** —
  64.7%, 67.4%, 54.9%, 41.4%. An entry-IV range that delivers 41% of a nominal 80% interval at
  two hours is not a slightly imprecise interval; it is the wrong width.
* BANKNIFTY at 120 minutes is the opposite failure: **both** models are over-dispersed at 92%,
  so the gate rejects the historical range there too. The gate is not a proxy for "prefer
  history".
* The single cell where `HIST` also wins CRPS (NIFTY 120) is the cell where `IV_ONLY`'s
  under-dispersion is most extreme, which is consistent with CRPS eventually punishing a
  badly-too-narrow interval — but one cell is one cell, and under this protocol it cannot be
  promoted to the primary horizon after the fact. A gate that rejects the better-scoring
model, and a score that prefers the worse-calibrated one, cannot both be the deciding criterion.
The protocol must say which governs **before** the trial starts. Three defensible choices:

1. **Calibration is a gate, CRPS ranks within it.** `HIST` would be adopted on NIFTY despite the
   worse CRPS. This is the reading implied by calling 70–90% a *gate*.
2. **CRPS decides, coverage is reported.** `IV_ONLY` would be adopted and its interval described
   as a 55% interval rather than an 80% one — honest, but then the stated nominal level is not
   the delivered one.
3. **Neither, until the interval is recalibrated.** The under-dispersion is systematic and large
   (NIFTY 60-minute width 56.9 against a realised-dispersion width of 92.8), so a widened
   IV-only range is a third candidate that this comparison has not scored. It is the option the
   evidence most directly suggests, and it is **out of scope here**: it would be a new model,
   and adding it after seeing these numbers is exactly the selection this protocol exists to
   prevent. It belongs in a separately declared comparison.

**Recommendation for the freeze: choice 1**, calibration as a gate with CRPS ranking inside it,
because a nominal 80% interval that delivers 55% is misdescribed rather than merely imprecise,
and because the gate was declared as a gate before these numbers were seen. On the development
evidence that recommendation would adopt the **historical** range at 60 minutes on NIFTY and
leave BANKNIFTY to CRPS, where `IV_ONLY` wins inside a gate both models pass — so the rule has
to tolerate the two indices answering differently, which is why they are kept separate
throughout and why no pooled verdict is defined.

## 8 · Development evidence, labelled as such

**12 and 16 November are development evidence** for this follow-up and are not part of the trial
sample. Reproduced here from the inputs this machine has:

**Unchanged frozen R7, 12 November, 60 minutes** (`premium_repricing_v2.py`, untouched):
NIFTY 246 matched rows, flat MAE 24.35, original MAE 24.41 → **SPLIT**; BANKNIFTY 251 matched
rows, flat 47.91, original 52.98 → **SPLIT**. Both SPLIT under the existing rules, as the 16
November check also expects.

**The 120-minute NIFTY counterexample is kept and reproduced**: `IV_ONLY_RAW` CRPS **31.69**
against the original **30.84**, with nominal 80% coverage **41.4%** on the 101-point grid. And
the finding that follows from keeping it: **recentring is not the explanation.** It moves CRPS by
0.17 and coverage by less than a point. The counterexample was never a location problem — it is a
width problem, and at the same horizon `HIST` covers 75.9% against IV-only's 41.4%.

**Not reproduced here: the 16 November checks.** The evening export
(`903225d9826dfbcac5011a099da1706f901854d40bc2e6d02d7ebdd43f9625e1`) is not on this machine, so
the 342-matched-rows / flat-20.78 / original-25.39 (NIFTY) and 58.56 / 66.49 (BANKNIFTY) figures,
and the common-centre 60-minute reproduction (NIFTY 14.37 vs 15.45, BANKNIFTY 41.31 vs 43.82),
are **unverified**. Running them needs that one file; everything else is in place and the command
is in the README.

## 9 · Scope

Premium ranges only. Quote calibration and coverage are reported **separately** from after-cost
Paper economics: the fee, fill and exit evidence a Paper decision needs lives in its own ledger
and lifecycle tests (`tools/premium_followon/`), and no number in this document is an
after-cost result. The frozen 60-minute pilot keeps its existing model, metrics, data rules and
16 November SPLIT result for both indices. Nothing here changes production ranking, gates,
sizing, exits, notifications or Paper access, and nothing here writes to a database.
