# PC-2 Batch 1 Jackknife Stability

Date: 2026-08-09
Repo: Marketapp-main-worktree
Scope: fallback-first percentile stability metadata

## Goal

Add deterministic jackknife stability metadata to the existing context-percentile payload without changing live gates or ranking authority.

## Implemented

- Added deterministic percentile quantile helper.
- Added IQR helper.
- Added jackknife threshold-stability helper:
  - method: `jackknife_iqr_threshold_v1`
  - recomputations: one leave-one-out sample per historical value
  - spread: IQR of leave-one-out threshold estimates
  - scale: IQR of the original history series
  - ratio: `spread / scale`
- Added PC-2 stability fields to each percentile cell:
  - `support_mode`
  - `stability_ratio`
  - `stability_spread`
  - `stability_scale`
  - `jackknife_n`
  - `stability_method`
  - `bar`
  - `stability_pass`
  - `switch_basis`
- Added VIX-derived stability bar:
  - source: `vix` 60-window cell
  - requires full 60 support
  - published as `support_policy.pc2_stability_bar`
- Added flattened variable fields:
  - `stability_ratio_30`
  - `stability_ratio_60`
  - `stability_bar_30`
  - `stability_bar_60`
  - `stability_pass_30`
  - `stability_pass_60`
  - `switch_basis_30`
  - `switch_basis_60`
  - `jackknife_n_30`
  - `jackknife_n_60`

## Behavior Boundary

- No live gate authority was changed.
- Existing percentile calculation still respects the existing `min_support` behavior.
- Existing bounded live ranking modifier remains unchanged.
- `switch_basis` is metadata only at this stage.
- Real live conversion still requires Batch 2 calibration and later gate wiring.

## Verification

- `python3 -m py_compile app/src/main/python/brain.py` passed.

## Next Batch

Batch 2 should create the calibration table for every KIND B constant and flag any calibrated percentile below 5 or above 95 for manual review.
