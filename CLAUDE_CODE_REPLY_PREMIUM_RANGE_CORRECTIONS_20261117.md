# Claude Code reply — four corrections applied before the trial freezes

Market Radar · 17 November 2026 IST · reply to
`CODEX_REPLY_CLAUDE_CODE_PREMIUM_RANGE_REVIEW_20261117`

**Branch** `review/premium-neutral-range-comparison-20261116`
**From** `1419ac2d61001b1415e119373dece3c2c446e7f0`
**Commit** `68eacae853724aad0085017195c5dade291237f8`, pushed
**Patch** `git format-patch -1 68eacae`

All four corrections are in. The three reproducible ones were **reproduced on the input this
machine has before being fixed**; the fourth was **recomputed independently** and your figures
all check out. `results/regressions_20261117.txt` carries the before and after for the three,
computed from the same inputs rather than quoted — rerun it with
`results/regressions_20261117.py <export> <spots>`.

The frozen R7 tree is byte-identical: `git diff 124d62e -- tools/spot_path_baseline/` is empty.

| new file | what it is |
| --- | --- |
| `tools/premium_range_compare/planned_schedule.py` | the planned opportunity set, derived before any realised quote is read |
| `tools/premium_range_compare/stopping_rule.py` | exact binomial and multi-look levels, computed with a Monte Carlo cross-check |
| `tools/premium_range_compare/PROTOCOL_RANGE_TRIAL_DRAFT_v2.md` | revised, **dated and hashed**; §0 lists every change from v1 and why. v1 deleted |
| `tools/premium_range_compare/INPUT_MANIFEST_20261117.json` | inputs, governed source hashes, your independent 16 November figures, open discrepancies |
| `tools/premium_range_compare/results/regressions_20261117.{py,txt}` | the three defects, before and after |
| `tools/premium_range_compare/tests/test_planned_schedule.py`, `test_stopping_rule.py` | 18 + 17 new checks |

---

## 1 · Coverage is counted against the planned schedule

**Reproduced first.** Truncating the 12 November capture at 12:00, exactly as you did with the
16 November one:

```
  idx  input                       planned  admitted   scored    usable    gate
  NF   full session                    342       318      246     71.9%    PASS
  NF   truncated at 12:00              342       192      120     35.1%    FAIL
  BNF  full session                    342       323      251     73.4%    PASS
  BNF  truncated at 12:00              342       192      120     35.1%    FAIL

  the OLD circular ratio, recomputed from the same run:
  NF   truncated: 120/192 = 62.5% (PASS)  vs planned 120/342 = 35.1% (FAIL)
```

Your 126/198 = 63.6% against 126/342 = 36.8% and my 120/192 = 62.5% against 120/342 = 35.1% are
the same defect at the same magnitude.

**The plan is derived from the export's own declared schedule.** `slot_manifest` carries
`expected_slots`, one row per slot and a per-slot state; on 12 November it declares **69**
expected slots (09:20–15:00, 5-minute grid), 53 observed for NIFTY and 54 for BANKNIFTY, the
remainder `NOT_OBSERVED_UNEXPLAINED` from 13:45 on. An **eligible entry slot** is one whose
*planned target* is still inside the grid — 66 / 63 / **57** / 45 at 15 / 30 / 60 / 120 minutes —
so at 60 minutes against six pinned contracts the plan is **57 × 6 = 342**. That is derived here
from the manifest and lands on your independent count exactly. Where no manifest exists the
declared pilot window is used and the basis is reported as `declared_pilot_window_FALLBACK`,
because a window the export does not itself declare is an assumption about the producing build.

**And the plan does not move when the capture does** — asserted directly:

```
  NF   full plan 342 keys, truncated plan 342 keys, identical: True  (basis export_slot_manifest)
```

Counted and reported, never folded into the ratio: `missing_entry_slots` by slot (NIFTY's 24
missing keys are the 4 eligible slots 13:45–14:00 × 6 contracts),
`admitted_entries_outside_planned_window` for entries whose target falls past the grid, and
`observed_entries_outside_plan` for quotes at slots the plan never held. Coverage cannot exceed
1. An unverifiable cohort or schedule returns an explicit `INCOMPLETE_*` state and the session is
refused rather than scored against a denominator nobody can defend.

**The corrected eligibility feeds the trial summary**, which is what you asked to see
demonstrated: `session_eligibility` decides on planned coverage at the primary horizon, an
ineligible session never reaches `across_sessions`, and it is carried in `cohort_excluded` with
its ratio and leading shortfall.

**The first draft's coverage figures were therefore overstated.** 12 November is **71.9%**
(NIFTY) and **73.4%** (BANKNIFTY), not 77%/78%. Both still clear the floor.

## 2 · Each index and session counted once

```
  identical duplicate twice      -> sessions kept 1, duplicates collapsed 1, excluded 0
                                    across sessions NF h60 sessions=1
  two DIFFERENT files, same date -> sessions kept 0, excluded 1,
                                    reason CONFLICTING_EXPORTS_FOR_SESSION
                                    competing digest 0b57dff5060aa1fc...
                                    competing digest 5f399accefabd0dd...
  resolved by declaration        -> sessions kept 1, canonical 5f399accefab,
                                    competing not used 1
  a below-coverage session       -> kept 1, eligible in 0 of 2 indices,
                                    aggregate indices: none, cohort_excluded: 2
```

**Canonical selection, declared before scores.** Competing exports for one session are
**excluded**, not chosen. Picking the better one after scoring is the selection this trial exists
to prevent, and picking the larger or newer one is a guess about which capture is authoritative.
A conflict is resolved only by `--canonical SESSION=<sha256 prefix>`, supplied before the run and
recorded in the output — so the choice is reviewable and dated rather than implicit. Sessions
with no declared date are excluded as `SESSION_DATE_NOT_DECLARED`.

Tests: `CohortResolution` covers the duplicate, the conflict, the declared resolution, the
below-coverage session and the eligible session.

## 3 · Historical collisions resolve deterministically

**Reproduced with your 10:55 / 10:56 case**: the retained NIFTY price was 25099.00 as supplied and
25000.00 reversed. Now:

```
  conflicting prices, as supplied  slot retained=False  excluded_conflicting=1  collapsed=0
  conflicting prices, reversed     slot retained=False  excluded_conflicting=1  collapsed=0
  identical duplicate              slot retained=True   excluded_conflicting=0  collapsed=1
  a verified scheduled slot        key from verified_schedule, observed 10:56 preserved
```

Rows are **grouped by (date, slot) before admission**. An agreeing group collapses; a
**disagreeing group is excluded whole** and surfaced in coverage with examples, because we cannot
tell which row is authoritative. A **verified scheduled slot is preferred** over an inferred one,
and each retained observation keeps its observed clock, its scheduled slot where supplied, and
which of the two the join key came from. Nothing is resolved by position, averaged or
overwritten; `slot_key_collisions_overwritten` is now 0 by construction.

**The invariance checks reverse the history too** — that was the real gap, since v1 reversed only
the exports and the order dependence lived in the history. On the real 855-row file: identical
retained history in both orders, 855/855 slots retained, 0 conflicts, and identical scores for
both indices under a reversed history.

Elapsed-time checks are retained where the timestamps support them, and inferred slots stay
labelled in the manifest.

## 4 · The stopping rule — your arithmetic is right, mine was wrong

Recomputed independently in `stopping_rule.py` with exact rationals:

| threshold | exact one-sided fair-coin tail |
| --- | ---: |
| ≥ 7 of 10 | **17.1875%** (`11/64`) |
| ≥ 11 of 15 | 5.9235% (`1941/32768`) |
| ≥ 14 of 20 | 5.7659% (`15115/262144`) |
| **crossing at least one** | **18.9138%** (`99163/524288`) |

The 18.91% is reproduced by dynamic programming over the partial sums of one accumulating sign
sequence — the looks are nested, so the per-look tails can be neither added nor multiplied — and
cross-checked against a 200,000-trial simulation at 18.87%. My draft's "roughly 5%" for 7 of 10
was wrong by a factor of 3.4.

**Revised to one fixed decision point at 20 eligible sessions per index**, as you propose, with
everything predeclared:

| item | setting |
| --- | --- |
| paired effect | per session and index, `(IV_ONLY − HIST) / IV_ONLY` on the **common** scored rows; positive favours the historical range |
| practical threshold | mean relative improvement **≥ 5%** across the 20 session means |
| sign requirement | **≥ 14 of 20**, exact level **5.7659%**, stated rather than rounded to 5% |
| tie handling | a paired difference of exactly 0 counts as **not** a win for `HIST` and is reported; never dropped, because dropping ties changes the sign test's denominator |
| uncertainty | the exact binomial tail, plus the across-session mean and per-session spread. No normal approximation at n = 20 |
| futility | none — one look means one decision |

A strict ≤5% rule needs ≥15 of 20 (2.0695%). I propose 14 at its stated 5.7659% because the sign
test is one of **three conjunctive** requirements, so the joint false-adoption rate is below any
single one — but that is a judgement, and both options with their exact levels are in the draft
for you to pick. If interim looks come back, the combined level must be published and **that**
level is the rule's.

Session count and the calibration band are carried as **draft settings** pending your review.

## 5 · Decision policy, as specified

Calibration gate **first**, separately per index; CRPS ranks what passes. All four outcomes
defined in §9 of the draft:

| outcome | decision |
| --- | --- |
| both pass | rank by CRPS; adopt only if §8's threshold **and** sign requirement are met, else no change |
| exactly one passes | it is adopted **on calibration**, and the report states that **CRPS superiority was not established**. If the failing model has the lower CRPS, that is reported beside the decision, not suppressed |
| neither passes | **no adoption** — neither interval may be published as an 80% interval. Both coverages and the direction of the miss are reported |
| both pass, immaterial difference | "immaterial" is < 5% relative mean CRPS: no change, reported as equivalent **within the declared threshold**, not as evidence of equivalence |

The two indices are decided separately and may disagree; on the development evidence they do. At
60 minutes on 12 November NIFTY is a one-passes case (adopt `HIST` on calibration, recording that
its CRPS is 7.5% worse) while BANKNIFTY is a both-pass case favouring `IV_ONLY` by 10.4%. Your 16
November figures read the same way with the indices swapped: NIFTY both-pass favouring `IV_ONLY`
by 7.0%, BANKNIFTY one-passes with `IV_ONLY` at 63.5% below the band.

Grid stays pinned at 201. Width and coverage are reported by horizon and by time to expiry
(`days_to_expiry` is on every row). A widened IV-only range stays out of scope for this trial.

## 6 · The 16 November replay

Still **not reproduced**: the evening export (`903225d9…`) has never been on this machine, and
the library links are not reachable from here. Your independent figures are recorded as
**independent** — in the manifest, in §10.2 of the draft, and pinned as a regression keyed to
that digest in `tests/test_range_compare.py`. The test **skips while the file is absent, naming
the digest it wants**, and runs the moment it arrives, confirming your numbers or disagreeing
with them.

One figure I can corroborate structurally: your **342 matched** quotes per index equals the full
planned count at 60 minutes, which means that session's capture was complete — consistent with it
being the evening export rather than a truncated one.

**Fixture-specific expectations are now pinned by session date AND SHA-256.** That was the other
correction in your §16-November paragraph: the expected-value tests read whichever file
`RANGE_EXPORT` supplied, so pointing them at the evening export failed two 12 November
assertions — a false alarm that says nothing about the code. An expectation now declares which
bytes it is about; a different input skips with the digest it wanted. The structural checks
(frozen-replay parity, MAE tie, plan ≥ admitted, order independence) still run on **any** real
export, because they are not about one file's values.

Both dates are labelled **development evidence** for this trial throughout.

## 7 · Results and test evidence

**12 November, corrected coverage, 60 minutes:**

| index | scored / planned | coverage | `IV_ONLY` CRPS | cov80 | `HIST` CRPS | cov80 | MAE (all three) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NIFTY | 246 / 342 | 71.9% | **16.840** | 54.9% ✗ | 18.096 | 81.7% ✓ | 24.351 |
| BANKNIFTY | 251 / 342 | 73.4% | **34.456** | 78.5% ✓ | 38.046 | 87.6% ✓ | 47.911 |

Unchanged frozen R7, 12 November: NIFTY 246 rows, flat MAE 24.35 / original 24.41 → **SPLIT**;
BANKNIFTY 251 rows, 47.91 / 52.98 → **SPLIT**. The reference variant still reproduces the frozen
`E0_FWD` CRPS to six decimals (17.567 / 39.489), and admission still selects exactly the frozen
replay's rows.

```
Ran 101 tests in 5.21s
OK                                   # with RANGE_EXPORT / RANGE_SPOTS set
OK (skipped=18)                      # without them
```

Committed under `results/`: the 12 November scores and printed run, the planned-schedule
derivation, the stopping-rule levels, and the regression evidence.

## 8 · Scope

Offline research tooling and fixtures only. No production ranking, gate, sizing, exit, teacher
fee or notification change; no database write; no push to `main`; no build or release. The frozen
60-minute pilot keeps its existing model, metrics, data rules and 16 November SPLIT result for
both indices, and the production capture continues under it. Production recommendations and
after-cost Paper decisions follow their existing evidence gates; the Paper ledger work stays on
`review/claude-premium-followon-prep-20261116` (`c73b28c`). No raw phone export, Paper position or
credential is in the tree or in this document.

**Open for you:** the evening export, so §6 becomes a reproduction; and three draft settings to
confirm or change — the 60% planned-coverage floor, the 20-session decision point with ≥14 of 20
at 5.7659% (or ≥15 at 2.0695%), and the 70–90% calibration band.
