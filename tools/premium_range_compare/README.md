# Neutral premium range comparison — IV-only against historical, one common centre

**Branch** `review/premium-neutral-range-comparison-20261116`, cut from the verified R7 replay
commit `124d62ee027941e43decc737e53d202c32b1d214`. Offline research tooling. Nothing here
collects, trades, writes to a database, changes a frozen rule, or touches the installed build,
Paper controls, v7 ranking, gates, sizing, exits or notifications. Every tool is read-only with
respect to its inputs.

## What is here

| file | what it is |
| --- | --- |
| `range_compare.py` | the runner: one centre, two candidate ranges, the frozen model as a reference, every refusal named, coverage against the **planned schedule**, cohort resolution, a deterministic historical join, and a quantile-grid sensitivity sweep |
| `planned_schedule.py` | the planned opportunity set, derived from **authoritative** capture-window rules and a versioned session calendar — never from the export's counts, rows or quotes. The export's slot manifest is validated **against** it |
| `stopping_rule.py` | exact binomial tails and the exact multi-look level, computed rather than asserted, with a Monte Carlo cross-check |
| `PROTOCOL_RANGE_TRIAL_DRAFT_v4.md` | the trial rules, dated and hashed, **ready to freeze** pending one input. §0 lists every change from v1, v2 and v3 with its reason, and the hashes are checked by a test |
| `tests/` | 142 checks across five modules, no network. `test_cli_smoke.py` runs every documented command as an **isolated subprocess**; `test_protocol_hashes.py` checks the protocol's claimed hashes against the files and the manifest |
| `results/` | the machine-readable scores and printed runs for 12 November, the planned-schedule derivation, the stopping-rule levels, and `regressions_20261117.{py,txt}` — the three defects reproduced before and after, from the same inputs |
| `INPUT_MANIFEST_20261117.json` | every input by SHA-256, the governed source hashes, the independent 16 November figures, and the discrepancies found |
| `tests/test_cli_smoke.py` | the documented `python3 -I` commands, run as subprocesses from the repository root |

The frozen R7 module is **read, never modified**: `range_compare.py` imports
`tools/spot_path_baseline/premium_repricing_v2.py` for the pricer, the clock, the parity forward,
the entry and exit selection rules, the pinned-contract resolver and the scorer.

## The exact invocation

```bash
# the 12 November comparison, as committed under results/
python3 -I tools/premium_range_compare/range_compare.py \
    <export_2026-11-12.json> <spots_20261021_20261104.jsonl> \
    --grid-sensitivity NF --out tools/premium_range_compare/results

# the planned schedule on its own, and the stopping-rule levels
python3 -I tools/premium_range_compare/planned_schedule.py <export.json>
python3 -I tools/premium_range_compare/stopping_rule.py --simulate 200000

# the three defects from the 17 November review, before and after
python3 -I tools/premium_range_compare/results/regressions_20261117.py \
    <export_2026-11-12.json> <spots_20261021_20261104.jsonl>

# the 16 November evening export, once it is available (nothing else is missing)
python3 -I tools/premium_range_compare/range_compare.py \
    <export_2026-11-16.json> <spots_20261021_20261104.jsonl> --out <dir>

# the unchanged frozen replay, for the reference numbers
python3 -I tools/spot_path_baseline/premium_repricing_v2.py \
    <export.json> <spots.jsonl> --out <dir>

# the tests. 7 of them need the real inputs:
cd tools/premium_range_compare && python3 -m unittest discover -s tests      # 142 run
RANGE_EXPORT=<export_2026-11-12.json> RANGE_SPOTS=<spots.jsonl> \
    python3 -m unittest discover -s tests          # 52 run, 0 skipped
```

Options: `--horizons 15,30,60,120` (60 is primary and is never chosen from the others),
`--slot-policy exact|floor5|explicit`, `--reverse-input` (results must be identical),
`--grid-sensitivity <INDEX>`, `--min-coverage 0.60`, `--canonical SESSION=<sha256>` (to resolve
competing exports for one session, declared before the run), `--json-only`, and one or more
exports for a multi-session run.

**Inputs are not committed.** Raw phone exports stay out of the repository; the manifest records
what each run read, by SHA-256, so a result can be tied to its input without the input being in
the tree.

## The result, in one table — 12 November, development evidence only

60 minutes, common scored rows, quantile grid 201:

| index | scored / planned | coverage | `FLAT` CRPS | `IV_ONLY` CRPS | `HIST` CRPS | `IV_ONLY` cov80 | `HIST` cov80 | MAE (all three) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NIFTY | 246 / 342 | 71.9% | 24.351 | **16.840** | 18.096 | 54.9% | 81.7% | 24.351 |
| BANKNIFTY | 251 / 342 | 73.4% | 47.911 | **34.456** | 38.046 | 78.5% | 87.6% | 47.911 |

The denominator is the **planned** schedule — 57 eligible entry slots × 6 pinned contracts, from
the export's own slot manifest — not the admitted quotes. Counting against admitted quotes let a
capture truncated at 12:00 report 62.5% and pass a 60% gate when its honest figure was 35.1%.

The plan comes from **authoritative rules** — the capture-window rules mirrored from
`PremiumCaptureCohort` and a session calendar whose holidays are read from the app's own
`brain.py` — so the export cannot move it. Its slot manifest is validated **against** that plan:
a slot outside the grid (`15:99`, `15:05`), a duplicate, a malformed row or no usable row makes
it `MANIFEST_PRESENT_BUT_INVALID`; rows that do not cover the grid, or declared counts that
disagree with it, make it `MANIFEST_DISAGREES_WITH_AUTHORITATIVE_SCHEDULE`. Either way the plan
is preserved, `diagnostic_coverage` is reported and `usable_coverage` is withheld because the
session cannot enter the cohort.

Cropping the manifest **and resetting both its totals** used to yield `VERIFIED` and
126/126 = 100%; it now reports 342 planned, 126 scored, 36.8% diagnostic, excluded.

`planned ≥ admitted` is **not** an invariant: a complete capture admits 69 × 6 = 414 entry quotes
against 342 sixty-minute opportunities, the 72 extra being late quotes whose targets fall past the
grid. What holds is `scored_in_plan ≤ observed_entries_in_plan ≤ planned_opportunities`.

Three things to read off it:

1. **The centring worked.** MAE is identical across the flat point and both ranges, to nine
   decimal places, at every horizon on both indices. That is the check that makes the CRPS column
   a comparison of shape rather than of location.
2. **IV-only scores better and calibrates worse, in 7 of 8 index/horizon cells each way.**
   `IV_ONLY` has the lower CRPS in 7 of 8 cells; `HIST` passes the proposed 70–90% calibration
   gate in 7 of 8 while `IV_ONLY` passes in 3 of 8. On NIFTY `IV_ONLY` is systematically
   under-dispersed and worse the longer the horizon — 64.7%, 67.4%, 54.9%, 41.4% at 15, 30, 60
   and 120 minutes. The two proposed criteria disagree almost everywhere and in the same
   direction, so this is structural rather than a one-horizon accident; §7.1 of the protocol
   tabulates all eight cells and recommends which criterion should govern.
3. **The 120-minute counterexample survives recentring.** `IV_ONLY_RAW` CRPS 31.69 against the
   frozen model's 30.84, coverage 41.4% on a 101-point grid — all three reproduced. Recentring
   moves CRPS by 0.17 and coverage by under a point, so the counterexample was never a location
   problem. It is a width problem.

## Two discrepancies found, both reported rather than smoothed over

* **The historical file has no scheduled-slot field.** The handoff states explicit scheduled-slot
  fields on 233 of 855 rows. The pinned file whose SHA-256 matches the handoff's own carries
  none; its only partially-present fields are recorded *directions*, non-null on **234** rows.
  Every join here therefore reads an inferred slot and is labelled so, and the `explicit` policy
  refuses this source by name rather than inferring quietly. 41 of 855 observation minutes are
  off the five-minute grid; flooring them collides with an existing observation 0 times.
* **The quoted and fitted IVs agree.** The handoff records a model-to-quote mismatch. On 12
  November, with the parity forward and the ACT/365 clock, the median difference is +0.01 vol
  points (NIFTY) and −0.01 (BANKNIFTY), with zero fit failures. The convention stays an open
  question; it is not visible in these rows. See protocol §3.1 for why the underlying reference,
  not the convention, is the likely source of earlier mismatch evidence.

## What is still blocked

The 16 November evening export (`903225d9…`) is not on this machine, so the two 16 November
development checks are **unverified**: the unchanged-R7 342-matched-rows / flat-20.78 /
original-25.39 (NIFTY) and 58.56 / 66.49 (BANKNIFTY) figures, and the common-centre 60-minute
reproduction (NIFTY 14.37 vs 15.45, BANKNIFTY 41.31 vs 43.82). That one file is the only missing
input; the command above runs both checks the moment it arrives.
