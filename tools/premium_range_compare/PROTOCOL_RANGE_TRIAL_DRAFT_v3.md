# Protocol draft v3 — does a historical premium range add value over an IV-only range?

**Status** DRAFT, **ready to freeze** pending one input · **Dated** 17 November 2026 IST ·
**Supersedes** `PROTOCOL_RANGE_TRIAL_DRAFT_v2.md` (deleted; §0 lists every change and why)

**Governs these files, by SHA-256 at the time of writing:**

| file | sha256 |
| --- | --- |
| `tools/premium_range_compare/range_compare.py` | `48447a5c6ef70888de09361ceec7597b8144830edb6b51d2c046ee2d73e7946b` |
| `tools/premium_range_compare/planned_schedule.py` | `5574866f490d50f1dd306b9fae6e4a270bb3850215452c45d45ac82d49caf9c4` |
| `tools/premium_range_compare/stopping_rule.py` | `19ce42fa589946ec76af152238652790e9a793885efbffd577bb9b10b89484ce` |
| `tools/spot_path_baseline/premium_repricing_v2.py` (frozen R7, **unmodified**) | `95e53e25d24ae0369f2c8d8ab63026a0cda499443ddae4041c6ee7ffb373233a` |

**Versions** comparison `premium_neutral_range_comparison_v2_planned_coverage` · schedule
`premium_range_planned_schedule_v1` · stopping rule `premium_range_stopping_rule_v1_exact` ·
R7 pricing contract `2.6` · **branch** `review/premium-neutral-range-comparison-20261116`,
base `124d62ee027941e43decc737e53d202c32b1d214`

---

## 0 · What changed, and why

### v2 → v3 (this revision)

| v2 | v3 | why |
| --- | --- | --- |
| a slot manifest whose header disagreed with its rows was **noted** and used anyway | the schedule is **refused** as `INCOMPLETE_SCHEDULE_MANIFEST_INCONSISTENT` and the session excluded (§4.1) | a manifest cropped at 12:00 declared 69 expected slots, carried 33 rows, and was accepted as `VERIFIED` with a 126-key denominator against 126 scored rows — **100% coverage for a capture that had lost half its session** |
| a refused schedule left the plan-relative counts computed against an empty plan | the ratio is **withheld** and the raw observed, scored and late counts are **preserved** (§4.1) | computing against an empty plan reads as "everything is outside the plan", which is not what happened |
| the trial invariant was `planned >= admitted` | `scored_in_plan ≤ observed_entries_in_plan ≤ planned_opportunities`, with late quotes checked separately (§4.2) | the old invariant is **false on a complete capture**: 16 November admits 414 quotes (69 slots × 6 contracts) against 342 sixty-minute opportunities, the 72 extra being late quotes whose targets fall outside the window |
| the documented `python3 -I` command failed | the sibling import is resolved explicitly, and a **subprocess smoke check** runs every documented command isolated from the repository root (§12) | isolated mode drops the script's own directory from `sys.path`; a unit test that imports the modules directly cannot catch it |
| sign requirement ≥ 14 of 20 at 5.7659% | **≥ 15 of 20 at 2.0695%** (§8) | Codex's choice: a comparative-superiority claim uses the threshold at or below 5%, not the nearest one above it |

### v1 → v2

| v1 | v2 | why |
| --- | --- | --- |
| coverage counted against entry candidates left **after** quote admission | counted against the **planned schedule** (§4) | the v1 denominator was circular: it shrank as a capture got worse. A capture truncated at 12:00 reported 62.5% and passed a 60% gate; the honest figure against the schedule is 35.1%. |
| one session entry per supplied file | **one per session**, identical inputs collapsed, competing exports excluded (§5) | supplying one session twice produced a two-session cohort, so a rule counted in sessions reached its threshold twice as fast on duplicated input |
| historical slot collisions resolved by position | grouped before admission; identical collapse, **conflicts excluded whole** (§6) | reversing the input changed the retained price — order dependence in the denominator of every historical scenario |
| interim looks at 10/15/20, "7 of 10 ≈ 5%" | **one fixed decision point at 20** sessions (§8) | 7 of 10 is **17.19%**, not 5%. The three looks together run at **18.91%** under fair signs. v1's nominal level was wrong by more than a factor of three. |
| CRPS vs the gate left to §7.1 to argue about | an explicit **decision policy** (§9) | Codex: use the calibration gate first, per index, and rank within it; say what happens in each of the three outcomes |
| 12 November coverage quoted as 77%/78% | **71.9% / 73.4%** | the v1 figures used the circular denominator. The corrected ones are lower, and both still clear the floor. |

Nothing about the comparison's *construction* changed: the centre, the two candidate ranges, the
fitted entry IV, the clock, the exit rule and the pinned grid are as in v1 (§1–§3, §7).

---

## 1 · The question, and the one thing it is not

**Primary question.** At 60 minutes, does a range built from prior sessions' moves at the same
time of day score better than a range built from the contract's own entry implied volatility,
when both are centred on the same observable entry price?

**It is not a question about the centre.** The centre is the entry bid/ask midpoint and every
variant uses it. **MAE is reported to confirm the separation worked**: with one centre the flat
point and both ranges must tie on MAE. If they do not, the centring is broken and the CRPS
comparison is not about shape. The runner asserts this tie on every real row at every horizon.

15, 30 and 120 minutes are reported separately and **never** used to pick the primary.
NIFTY and BANKNIFTY are separate throughout, and no pooled verdict is defined.

## 2 · The variants

| variant | centre | dispersion from | recentred | role |
| --- | --- | --- | --- | --- |
| `FLAT` | entry mid | nothing | n/a | the floor a range must beat |
| `IV_ONLY` | entry mid | the contract's entry IV, **fitted from its own midpoint** under the parity forward, on a fixed 201-point quantile grid | yes | candidate |
| `HIST` | entry mid | prior sessions' log returns over the same clock interval, **median removed** | yes | candidate |
| `R7_E0_FWD` | — | prior sessions' raw returns at the **quoted** IV | no | reference against the unchanged frozen replay |
| `IV_ONLY_RAW`, `HIST_RAW` | — | as above | no | diagnostics: what the recentring actually changed |

Both candidates are repriced with the **same** fitted entry IV, the same ACT/365 clock and the
same bounds, so the only difference between them is the scenario distribution. The median
historical return is removed from `HIST` so an empirical drift cannot do the centring's job and
be reported as added value.

## 3 · What is computable at entry, and nothing else

* **Forward** — put-call parity on matched call/put midpoints in the same expiry, strike and
  **acquisition** (the frozen `implied_forwards`): ≥2 strikes per `(underlying, expiry)` group,
  maximum cross-strike dispersion 25 points, median of the per-strike estimates. A contract may
  use only the forward of **its own entry acquisition**.
* **Entry IV** — fitted by bisection so the frozen pricer reproduces the contract's own midpoint.
  The **quoted** IV is recorded beside it and never substituted for a failed fit. Named
  failures, never clamped to a bracket endpoint: `iv_target_below_floor`,
  `iv_target_above_ceiling`, `iv_no_convergence`, `iv_inputs_invalid`.
* **Clock** — one ACT/365 calendar term, expiry 15:30 IST on each contract's own expiry date,
  entry at the entry acquisition's receipt, target at entry receipt + horizon. The scenario's
  variance horizon is the hold, `T_entry − T_target` years.
* **Exit** — the exact same contract, the quote whose cutoff is nearest the **planned** target,
  admitted only within ±5 s. Identical across every variant and horizon.
* **Event time** — the export carries the app's acquisition receipt only. Exchange quote event
  time is **unverified** (`clock_event_time_verified` is 0 on every row of the 12 November
  export), and that bounds every number here.

### 3.1 · The broker-convention question is open; the current evidence shows no mismatch

On 12 November, with the parity forward and the ACT/365 clock, quoted and fitted IV agree:
median difference **+0.010** vol points (NIFTY, 246 rows; −0.645 to +0.900) and **−0.006**
(BANKNIFTY, 251 rows; −0.467 to +0.559), with **zero** fit failures. The likeliest source of
earlier mismatch evidence is the underlying reference, not the convention: R7's own 2.0
correction found index spot with `r = 0` gave entry basis errors of 8.27 (NIFTY) and 58.67
(BANKNIFTY) points, **antisymmetric in calls and puts**, removed by the parity forward.

## 4 · Coverage — against the planned schedule

**The denominator is the plan, never the admitted quotes.** `planned_schedule.py` derives it
before any realised quote is read:

1. **preferred basis** — the export's own `slot_manifest`: `expected_slots`, one row per slot,
   a per-slot state. The 12 November export declares **69** expected slots (09:20–15:00 on a
   5-minute grid), 53 observed for NIFTY and 54 for BANKNIFTY, the rest
   `NOT_OBSERVED_UNEXPLAINED` from 13:45 on;
2. **fallback basis** — the declared pilot capture window, reported as `FALLBACK` because a
   window the export does not itself declare is an assumption about the producing build.

An **eligible entry slot** is one whose *planned target* is still inside the grid. On a 69-slot
grid: 66 at 15 min, 63 at 30, **57 at 60**, 45 at 120. The plan is the cross product of eligible
slots and pinned contract identities, so at 60 minutes with six pinned contracts it is
**57 × 6 = 342** — the figure Codex derived independently for 16 November, and the figure
`planned_schedule.py` derives from the 12 November manifest.

```
planned opportunities  (eligible slots × pinned contracts)
  → admitted entry quote                      else counted in missing_entry_slots, by slot
  → matched exit within ±5 s                  else rej_no_exit_quote_within_tolerance
  → target before expiry                      else rej_target_at_or_after_expiry
  → parity forward in the entry acquisition   else rej_no_forward_in_entry_acquisition_group
  → fitted entry IV                           else rej_iv_target_below_floor / _above_ceiling
  → ≥ 5 prior sessions with both endpoints    else rej_insufficient_prior_sessions  (HIST only)
  → scored inside the plan
usable coverage = scored_in_plan / planned_opportunities
```

Counted **separately**, never credited to coverage: admitted entries whose planned target falls
past the grid (`admitted_entries_outside_planned_window`), and observed entries for slots the
plan never contained (`observed_entries_outside_plan`). Coverage cannot exceed 1.

**An explicitly incomplete state refuses the session rather than scoring it:**
`INCOMPLETE_COHORT_NOT_RESOLVED`, `INCOMPLETE_PINNED_COUNT_DISAGREES_WITH_COHORT`,
`INCOMPLETE_NO_SCHEDULE_BASIS`, `INCOMPLETE_NO_ELIGIBLE_SLOT_FOR_THIS_HORIZON`,
`INCOMPLETE_SCHEDULE_MANIFEST_INCONSISTENT` (§4.1).

### 4.1 · A present but inconsistent manifest is incomplete, not usable

The manifest is validated **against its own rows** before any denominator is built from it, and
against **distinct valid slot keys** rather than the raw row count — so a malformed or
duplicated row cannot make the arithmetic add up while a real slot is missing. The schedule is
refused when:

| check | audit field |
| --- | --- |
| `expected_slots` disagrees with the distinct valid slot rows | `declared_expected` vs `distinct_valid_slots` |
| a row has no slot, or one that will not parse | `malformed_rows` |
| a row repeats a slot key already seen | `duplicate_rows` |
| `observed_slots` exceeds the distinct valid rows | — |

On a refusal the **coverage ratio is withheld** — not computed against an empty plan, which
would read as "everything is outside the plan" — while the **raw counts are preserved**:
`observed_entries_total`, `scored_total` and
`admitted_entries_outside_planned_window`. Valid late and missing quote counts therefore survive
the refusal, and the session is excluded from the cohort with the reason and the audit attached.

Measured on 12 November, both indices, the three cases that must behave differently:

| input | schedule state | planned | scored | usable | observed |
| --- | --- | ---: | ---: | ---: | ---: |
| full export (NIFTY) | `VERIFIED` | 342 | 246 | 71.9% | 318 |
| quotes cropped at 12:00 | `VERIFIED` | 342 | 120 | **35.1% → excluded** | 192 |
| quotes **and** manifest cropped | `INCOMPLETE_SCHEDULE_MANIFEST_INCONSISTENT` | 0 | — | **WITHHELD → excluded** | 192 |
| a duplicate manifest row | `INCOMPLETE_SCHEDULE_MANIFEST_INCONSISTENT` | 0 | — | WITHHELD | 318 |
| a malformed manifest row | `INCOMPLETE_SCHEDULE_MANIFEST_INCONSISTENT` | 0 | — | WITHHELD | 318 |

### 4.2 · The coverage invariant

```
scored_in_plan  ≤  observed_entries_in_plan  ≤  planned_opportunities
observed_entries_total  =  observed_entries_in_plan + observed_entries_outside_plan
```

**`planned_opportunities ≥ admitted quotes` is NOT an invariant.** A complete capture admits one
quote per contract per captured slot — 69 × 6 = **414** on 16 November — while only 342 of those
are sixty-minute opportunities. The 72 extra are **late quotes** whose planned targets fall past
the end of the grid: counted in `admitted_entries_outside_planned_window`, never a coverage
shortfall and never an error.

**Minimum usable coverage: 60% of planned, per index, at the primary horizon** (proposed). The
corrected 12 November figures are **71.9%** (NIFTY, 246/342) and **73.4%** (BANKNIFTY, 251/342).
A session below the floor is reported with its ratio and its leading shortfall and excluded
**whole** — never partially counted. An incomplete session is not a failed model.

**Every variant's own row count is reported beside the common set**, and the headline comparison
uses only rows where `FLAT`, `IV_ONLY` and `HIST` all scored.

## 5 · The cohort — one entry per index and session

| input | outcome |
| --- | --- |
| the same bytes supplied N times, by SHA-256 | **one** session; `duplicate_inputs_collapsed = N−1` reported |
| two different files claiming the same session date | **excluded**: `CONFLICTING_EXPORTS_FOR_SESSION`, both digests reported |
| a conflict with an explicit declaration | resolved **only** by `--canonical SESSION=<sha256 prefix>`, declared before the run and recorded in the output |
| no session date declared | excluded: `SESSION_DATE_NOT_DECLARED` |
| below the coverage floor, or an incomplete schedule | kept and reported, excluded from the aggregate, carried in `cohort_excluded` |

**No export is chosen by size, age or score.** Picking the "better" file after scoring is exactly
the selection this trial exists to prevent, and picking the larger or newer one is a guess about
which capture is authoritative. A canonical choice must be declared, dated and reviewable.

Excluded sessions never vanish: they appear in `inputs_excluded` or `cohort_excluded` with their
reason, and the run prints them.

## 6 · The historical join

| what | rule |
| --- | --- |
| slot | `exact` (default) reads the observation's own minute, as the frozen replay does. `floor5` floors to a 5-minute slot as a reproducible **sensitivity**, labelled inferred. `explicit` reads the source's own scheduled-slot field and **fails closed** when it has none. A **verified scheduled slot is preferred** over an inferred one wherever present, and the choice is recorded per observation. |
| collisions | rows are **grouped by (date, slot) before admission**. A group agreeing on every field a scenario reads **collapses** (`collapsed_identical_duplicates`). A group that **disagrees is excluded whole** (`excluded_conflicting_slots`, with examples), because we cannot tell which row is authoritative. Nothing is resolved by position, nothing is averaged, nothing is overwritten. |
| provenance | each retained observation keeps its observed clock, its scheduled slot when supplied, and which of the two the join key came from (`slot_from`). |
| endpoints | a delayed, duplicate or missing endpoint is **counted, never interpolated**: `hist_missing_entry_endpoint`, `hist_missing_exit_endpoint`, `hist_non_positive_price`, `hist_non_finite_return`, `hist_pairs_used`. |
| window | the **rebuilt prior window is fixed** for the first controlled comparison: the pinned 855-row file, SHA-256 `f6f29ed8…`, 11 sessions 21 Oct – 4 Nov 2026. A rolling-history variant is listed separately and needs its timestamped inputs and update rule specified first. |
| invariance | **both** the exports and the history are reversed in the order-independence checks. v1 reversed only the exports, and the history was where the order dependence lived. |

### 6.1 · A discrepancy still open

The original handoff states the review package has explicit scheduled-slot fields on **233 of
855** historical rows. The pinned file whose SHA-256 matches
(`f6f29ed8fd6e1290f3d0f59e9be2cdd36f236b945409aebb2a73edffd499d186`, 855 rows) carries **none**.
Its fields are `d`, `t`, `nf`, `bnf`, `app_brain_version`, `rec_nf_dir`, `rec_bnf_dir`; the only
partially-present ones are the recorded **directions**, non-null on **234** rows, all
2026-11-02…04, sampled value `UNAVAILABLE`. A recorded direction is not a scheduled slot, and 234
is not 233. Until it is resolved every join here reads an **inferred** slot and says so, and the
`explicit` policy refuses this source by name. Measurable on the file: **41 of 855** observation
minutes are off the 5-minute grid; flooring them collides with an existing on-grid observation
**0** times, and on the real file the retained history is identical in both input orders with
**0** conflicting slots.

## 7 · Scoring, and the grid the gate depends on

CRPS for the range; nominal **80%** coverage and mean interval width reported **together**; MAE to
confirm the central tie. All four come from the frozen `R7.score`.

**The grid is pinned at 201 points.** CRPS is insensitive to it — NIFTY at 120 minutes moves
31.732 → 31.686 across N = 21…401, a 0.15% spread — but nominal coverage is not: 35.6, 37.4,
38.5, 39.1, **41.4**, 42.0, 42.0 percent at N = 21, 31, 41, 51, 101, 201, 401. A six-point swing
is wider than the gap between a passing and a failing gate, so **a protocol that gates on
coverage must pin the grid.** 201 is the smallest grid at which coverage is converged and CRPS is
converged to four significant figures. `width80` creeps up slowly with N — the 10th and 90th
percentiles of a discrete grid approach the continuous quantiles from inside — so width is
reported **with the grid pinned** and never compared across grids. `HIST` is grid-independent by
construction. (41.4% is exactly the 101-point grid, which reconciles the earlier figure.)

**Coverage and width are reported by horizon and by time to expiry**, not only pooled;
`days_to_expiry` is carried on every row for that breakdown.

## 8 · The stopping rule — one fixed decision point

**v1's rule was wrong.** It called 7 of 10 paired session wins "the one-sided sign test at
roughly 5%". Computed exactly (`stopping_rule.py`, checked against an independent Monte Carlo):

| threshold | exact one-sided fair-coin tail |
| --- | ---: |
| ≥ 7 of 10 | **17.19%** (`11/64`) |
| ≥ 11 of 15 | 5.92% (`1941/32768`) |
| ≥ 14 of 20 | 5.77% (`15115/262144`) — **not used**: above 5% |
| **≥ 15 of 20** | **2.07%** (`5425/262144`) — **the adopted threshold** |
| **crossing at least one of the three** | **18.91%** (`99163/524288`) |

The looks are nested on one accumulating sign sequence, so the per-look tails can be neither
added nor multiplied; 18.91% is exact by dynamic programming over partial sums and agrees with a
200,000-trial simulation (18.87%). A rule with a nominal 5% level and an actual 18.91% level is
not a 5% rule.

**The revision: ONE fixed decision point at 20 eligible sessions per index.** No interim
adoption. Predeclared, as Codex asks:

| item | setting |
| --- | --- |
| **paired effect** | per session and index, the relative mean-CRPS difference `(IV_ONLY − HIST) / IV_ONLY` on the **common** scored rows. Positive means the historical range is better. |
| **practical threshold** | mean relative improvement **≥ 5%** across the 20 session means |
| **sign requirement** | `HIST` better in **≥ 15 of 20** sessions, exact one-sided level **2.0695%** (`5425/262144`) under independent fair signs. This is the threshold at or below 5%; ≥ 14 of 20 would be 5.7659%, above it. Settled per Codex's instruction: a comparative-superiority claim uses the threshold that meets the level, not the nearest one above it. |
| **tie handling** | a session whose paired difference is exactly 0 counts as **not** a win for `HIST` and is reported; it is never dropped, because dropping ties changes the denominator of the sign test |
| **uncertainty** | the exact binomial tail above, plus the across-session mean and the per-session spread. No normal approximation is used at n = 20. |
| **unit** | the **session mean** per index. Rows within a session are not independent — neighbouring strikes and overlapping windows see the same few underlying moves — and the decision points are counted in sessions. |
| **eligibility** | only sessions passing §4's coverage floor and §5's cohort rules count toward the 20 |
| **futility** | none. One look means one decision; a trial stopped early for futility is an interim look and would need its own published level. |

If interim decisions are reintroduced, the combined level **must** be published, computed as
above, and that combined level — not the per-look level — is the rule's.

## 9 · Decision policy

**The calibration gate first, separately for NIFTY and BANKNIFTY; CRPS ranks what passes.**

| outcome | decision |
| --- | --- |
| **both pass the gate** | rank by mean CRPS on common rows. Adopt the better **only if** §8's threshold and sign requirement are met; otherwise **no change**, and the result is reported as "both calibrated, difference not established". |
| **exactly one passes** | the passing model is described as **qualifying on calibration**, and **both** CRPS results are reported. **A calibration pass alone establishes no comparative CRPS result** — the words are Codex's and they are the rule, not a caveat. If the failing model has the lower CRPS, that is reported beside the decision, not suppressed. |
| **neither passes** | **no selection is recorded.** Neither interval delivers its nominal level, so neither may be published as an 80% interval. The session set is reported with both coverages and the direction of the miss (too narrow or too wide). |
| **both pass and the difference is immaterial** | "immaterial" is **< 5%** relative mean CRPS, i.e. §8's threshold not met: **no change**, reported as equivalent within the declared threshold. Not as evidence of equivalence. |
| **the two indices disagree** | they are decided separately. No pooled verdict exists, and a disagreement is a result, not a tie to be broken. |

The gate band is **70%–90%** nominal 80% coverage (proposed, pending review). A widened IV-only
range is **out of scope** for this trial and belongs in a separately declared comparison: adding
a model after seeing these numbers is the selection this protocol exists to prevent.

## 10 · Development evidence, labelled as such

**12 and 16 November are development evidence** for this trial and are not part of the sample.

### 10.1 · 12 November, reproduced on this machine

Corrected coverage, 60 minutes: NIFTY **246/342 = 71.9%**, BANKNIFTY **251/342 = 73.4%**; both
eligible. Unchanged frozen R7: NIFTY 246 matched rows, flat MAE 24.35, original 24.41 →
**SPLIT**; BANKNIFTY 251 rows, 47.91 / 52.98 → **SPLIT**.

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

`IV_ONLY` has the lower CRPS in **7 of 8** cells; `HIST` passes the gate in **7 of 8**,
`IV_ONLY` in **3 of 8**. Under §9: at 60 minutes NIFTY is a one-passes case (adopt `HIST` on
calibration, recording that its CRPS is worse), and BANKNIFTY is a both-pass case where the 10.4%
CRPS difference favours `IV_ONLY` — so on this evidence **the two indices decide differently**,
which §9 permits by design.

**The 120-minute counterexample is kept and reproduced**: `IV_ONLY_RAW` CRPS **31.69** against
the original **30.84**, nominal 80% coverage **41.4%** on the 101-point grid. Recentring moves
CRPS by 0.17 and coverage by under a point, so it was never a location problem — it is a width
problem, and `HIST` covers 75.9% at the same horizon.

### 10.2 · 16 November, independent — not reproduced here

Reported by Codex, who **reran the corrected runner** on the evening export
(`903225d9826dfbcac5011a099da1706f901854d40bc2e6d02d7ebdd43f9625e1`) on 17 November 2026. That
file **has never been on this machine**, so these remain independent evidence. Codex reports the
corrected runner scored **342 of 342 planned sixty-minute rows per index — 100% usable coverage**
— with scores matching the prior replay:

| index, 60 min | matched | `IV_ONLY` CRPS | `HIST` CRPS | `IV_ONLY` cov80 | `HIST` cov80 | MAE (all common-centre variants) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| NIFTY | 342 | 14.369 | 15.450 | 74.0% | 81.0% | 20.779 |
| BANKNIFTY | 342 | 41.306 | 43.819 | 63.5% | 78.1% | 58.562 |

Unchanged R7 again SPLIT on both indices. These figures are **pinned as a regression**, keyed to
that digest, in `tests/test_range_compare.py`; the test skips while the file is absent and runs
the moment it arrives, confirming them or disagreeing.

Under §9 they read: NIFTY **both pass** (74.0% and 81.0%) with `IV_ONLY` better on CRPS by 7.0%;
BANKNIFTY **one passes** — `HIST` at 78.1%, `IV_ONLY` at 63.5% below the band — so `HIST`
qualifies on calibration there and no comparative CRPS result is claimed, even though `IV_ONLY`
scores 5.7% better. That is the same structural disagreement between the indices as 12 November,
with the sides swapped, which §1's separate treatment of the two indices exists for.

Codex's 342 matched quotes per index also **corroborates the planned count independently**: 342
is the full sixty-minute plan, so that capture was complete, and the 414 admitted quotes Codex
reports are 69 × 6 with 72 late — exactly the shape §4.2's invariant describes.

## 12 · The commands this protocol is run with

Every documented command must run **isolated**, from the repository root, in the form the
environment prescribes for a tool that reads supplied data:

```bash
python3 -I tools/premium_range_compare/range_compare.py <export...> <spots> [--out DIR]
python3 -I tools/premium_range_compare/planned_schedule.py <export>
python3 -I tools/premium_range_compare/stopping_rule.py [--simulate N]
python3 -I tools/premium_range_compare/results/regressions_20261117.py <export> <spots>
```

`python3 -I` drops the script's own directory from `sys.path`, so sibling imports are resolved
explicitly in each module rather than relying on it. `tests/test_cli_smoke.py` runs each of these
as a **subprocess**, isolated, including a multi-export run that exercises the cohort rule — a
unit test that imports the modules directly cannot catch an isolated-mode import failure,
because by then the path is already set up.

## 11 · Scope

Premium forecast ranges only. Quote calibration and coverage are reported **separately** from
after-cost Paper economics; the fee, fill and exit evidence a Paper decision needs lives in
`tools/premium_followon/` with its own ledger and lifecycle tests, and no number here is an
after-cost result. The frozen 60-minute pilot keeps its existing model, metrics, data rules and
16 November SPLIT result for both indices. Production recommendations follow their existing
evidence gates. Nothing here changes production ranking, gates, sizing, exits, notifications or
Paper access, and nothing here writes to a database.
