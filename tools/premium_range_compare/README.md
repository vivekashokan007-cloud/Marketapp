# Neutral premium range comparison — IV-only against historical, one common centre

**Branch** `review/premium-neutral-range-comparison-20261116`, cut from the verified R7 replay
commit `124d62ee027941e43decc737e53d202c32b1d214`. Offline research tooling. Nothing here
collects, trades, writes to a database, changes a frozen rule, or touches the installed build,
Paper controls, v7 ranking, gates, sizing, exits or notifications. Every tool is read-only with
respect to its inputs.

## What is here

| file | what it is |
| --- | --- |
| `range_compare.py` | the runner: one centre, two candidate ranges, the frozen model as a reference, every refusal named, a coverage ledger built from intended opportunities, and a quantile-grid sensitivity sweep |
| `PROTOCOL_RANGE_TRIAL_DRAFT_v1.md` | the trial rules, written to be frozen **before** the first scored fresh export: the question, the variants, the ledger, the decision rule, the repeated-looks rule, and the two discrepancies that must be resolved first |
| `tests/test_range_compare.py` | 52 checks, no network. 45 run anywhere; 7 need the real inputs and skip loudly without them |
| `results/range_comparison_2026-11-12.{json,txt}` | the machine-readable scores and the printed run for 12 November, both indices, all four horizons, plus the grid sweep |
| `INPUT_MANIFEST_20261116.json` | every input by SHA-256, including the one that is **absent**, and the discrepancies found in the ones that are present |

The frozen R7 module is **read, never modified**: `range_compare.py` imports
`tools/spot_path_baseline/premium_repricing_v2.py` for the pricer, the clock, the parity forward,
the entry and exit selection rules, the pinned-contract resolver and the scorer.

## The exact invocation

```bash
# the 12 November comparison, as committed under results/
python3 -I tools/premium_range_compare/range_compare.py \
    <export_2026-11-12.json> <spots_20261021_20261104.jsonl> \
    --grid-sensitivity NF --out tools/premium_range_compare/results

# the 16 November evening export, once it is available (nothing else is missing)
python3 -I tools/premium_range_compare/range_compare.py \
    <export_2026-11-16.json> <spots_20261021_20261104.jsonl> --out <dir>

# the unchanged frozen replay, for the reference numbers
python3 -I tools/spot_path_baseline/premium_repricing_v2.py \
    <export.json> <spots.jsonl> --out <dir>

# the tests. 7 of them need the real inputs:
cd tools/premium_range_compare && python3 -m unittest discover -s tests
RANGE_EXPORT=<export_2026-11-12.json> RANGE_SPOTS=<spots.jsonl> \
    python3 -m unittest discover -s tests          # 52 run, 0 skipped
```

Options: `--horizons 15,30,60,120` (60 is primary and is never chosen from the others),
`--slot-policy exact|floor5|explicit`, `--reverse-input` (results must be identical),
`--grid-sensitivity <INDEX>`, `--json-only`, and one or more exports for a multi-session run.

**Inputs are not committed.** Raw phone exports stay out of the repository; the manifest records
what each run read, by SHA-256, so a result can be tied to its input without the input being in
the tree.

## The result, in one table — 12 November, development evidence only

60 minutes, common scored rows, quantile grid 201:

| index | rows | `FLAT` CRPS | `IV_ONLY` CRPS | `HIST` CRPS | `IV_ONLY` cov80 | `HIST` cov80 | MAE (all three) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NIFTY | 246 | 24.351 | **16.840** | 18.096 | 54.9% | 81.7% | 24.351 |
| BANKNIFTY | 251 | 47.911 | **34.456** | 38.046 | 78.5% | 87.6% | 47.911 |

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
