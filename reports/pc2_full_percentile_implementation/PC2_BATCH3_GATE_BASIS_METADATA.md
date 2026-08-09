# PC2 Batch 3 - Gate Basis Metadata

## Scope

Batch 3 adds attribution metadata for percentile-capable gates. It does not change live pass/fail behavior.

The goal is to make every later replay and post-close evaluation able to answer:

- which old hard constant was used
- what percentile target was calibrated for that constant
- whether a same-day 60-window percentile cell existed
- whether the percentile cell passed the jackknife stability bar
- whether the current row was still evaluated by hard fallback or only had a percentile counterfactual available

## Live Behavior

- Live gate behavior remains unchanged.
- `gate_basis` is currently `hard_fallback`.
- `live_behavior_change` is `false`.
- `counterfactual_basis` is populated only when the relevant percentile cell passes stability.
- Batch 5 is still required before any percentile gate becomes authoritative.

## Percentile Candidate Gates

The following hard constants now have metadata wiring:

| Constant | Gate | Percentile Target | Support | Status |
| --- | --- | ---: | ---: | --- |
| `MIN_CREDIT_RATIO` | `credit_ratio_below_floor` | 14.64 | 9728 | eligible after stability pass |
| `IV_RICH_MIN` | `iv_not_rich` | 0.62 | 2647 | owner-approved extreme percentile |
| `MIN_PROB` | `credit_prob_below_floor` / `prob_below_floor` | 44.88 | 2756 | eligible after stability pass |
| `MIN_SIGMA_OTM` | `sigma_otm_too_close` | 17.10 | 9953 | eligible after stability pass |
| `MAX_SIGMA_OTM` | `sigma_otm_too_far` | 39.86 | 9953 | eligible after stability pass |

## IV_RICH_MIN Decision

`IV_RICH_MIN = 1.15` calibrated to the 0.62 percentile in the local generated-candidate evidence. This means the old hard IV richness floor was not a normal market-context threshold. It was an extreme floor that could suppress valid credit structures during low-IV regimes.

The user explicitly approved including `IV_RICH_MIN` in the percentile program because the old calculation/interpretation was wrong. The implementation records it with:

- `activation_status = percentile_candidate_after_stability_pass`
- `pct_target_review_flag = owner_approved_extreme_percentile`

This preserves audit clarity while allowing the later Batch 5 live wiring to treat IV richness as market-context-relative rather than as a blind fixed floor.

## Applicability Guard

Accepted candidates are stamped only when the candidate has the relevant source metric and is a credit structure:

- `MIN_CREDIT_RATIO` requires `creditWidthRatio` or `credit_width_ratio`.
- `IV_RICH_MIN` requires `ivRichness` or `iv_richness`.
- `MIN_PROB` requires `probProfit`, `prob_profit`, or `prob`.
- `MIN_SIGMA_OTM` and `MAX_SIGMA_OTM` require `sigmaOTM` or `sigma_otm`.
- Debit candidates are not stamped with credit-gate metadata.

Rejected candidates are stamped only when their `rejection_stage` maps to a PC2 gate.

## Data Shape

Generated / chosen candidate payloads can now include:

- `pc2_gate_basis`
- `gate_basis_summary`

Rejected candidate and rejected outcome payloads can now include:

- `gate_basis`
- `pc2_gate_basis`
- `pct_target`
- `slice_key`
- `basis_support_count`
- `basis_stability_ratio`
- `basis_stability_bar`
- `basis_stability_pass`
- `counterfactual_basis`

No new Supabase table column is required by this batch. These fields are JSON payload evidence and are kept through the Java compactors.

## Files Changed

- `app/src/main/python/brain.py`
- `app/src/main/java/com/marketradar/app/NativeBridge.kt`
- `app/src/main/java/com/marketradar/app/MarketMLService.kt`
- `tools/pc2_calibrate_kind_b.py`
- `reports/pc2_full_percentile_implementation/PC2_BATCH2_CALIBRATION_REPORT.md`
- `reports/pc2_full_percentile_implementation/PC2_BATCH2_CALIBRATION_TABLE.csv`

## Verification

Ran:

```bash
python3 -m py_compile app/src/main/python/brain.py tools/pc2_calibrate_kind_b.py
git diff --check
python3 tools/pc2_calibrate_kind_b.py
```

Results:

- Python compile passed.
- Diff whitespace check passed.
- Calibration regenerated successfully.
- Calibration source counts:
  - B1 rows: `1654`
  - C3 rows: `168620`
  - generated rows: `11036`

## Next Batches

Recommended sequence:

1. Batch 4: G0 delete-proof / non-candidate threshold inventory.
2. Batch 6: premium-history / daily-history backfill hygiene for stable context.
3. Batch 5: live gate-basis switch for eligible gates, including `IV_RICH_MIN`, only after evidence and attribution are present.

