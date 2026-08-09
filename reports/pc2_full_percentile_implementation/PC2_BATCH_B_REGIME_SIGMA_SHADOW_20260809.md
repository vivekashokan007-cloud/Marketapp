# PC2 Batch B - VIX Regime and Sigma Context Shadow

Date: 2026-08-09

## Scope

This batch records the next PC2 percentile-candidate group in the live snapshot artifact without changing strategy generation, rejection, ranking, notification, or exit behavior.

No Supabase calls were made.

## Constants Covered

| Constant | Current value | Current role | Batch B authority | Live softened |
| --- | ---: | --- | --- | --- |
| IV_HIGH | 20 | VIX regime routing and IV force scoring | delete_proof_required | No |
| IV_VERY_HIGH | 24 | Very-high VIX debit co-primary routing | delete_proof_required | No |
| IV_LOW | 15 | Low VIX debit/credit force scoring | delete_proof_required | No |
| SIGMA_IMPORTANT_THRESHOLD | 2.0 | Significant-move notification threshold | context_measure_first | No |
| SIGMA_ENTRY_THRESHOLD | 1.5 | Sigma entry policy constant | context_measure_first | No |
| SIGMA_EXIT_THRESHOLD | 1.0 | Sigma exit policy constant | context_measure_first | No |

## What Changed

Added a snapshot-only inventory:

- `PC2_BATCH_B_REGIME_SIGMA_VERSION = pc2_batch_b_regime_sigma_shadow_v1`
- `_pc2_batch_b_regime_sigma_inventory()`
- `market_forces.pc2_batch_b_regime_sigma`
- `poll_summary.pc2_batch_b_regime_sigma_status`
- `poll_summary.pc2_batch_b_regime_sigma_shadow_count`
- `poll_summary.pc2_batch_b_regime_sigma_live_softened_count`
- `snapshot_context.snapshot_pc2_batch_b_regime_sigma`

## What Did Not Change

- `_get_varsity_filter()` still uses absolute `IV_HIGH`, `IV_VERY_HIGH`, and `IV_LOW`.
- `_assess_force3()` still uses absolute VIX thresholds with percentile context as an additional signal.
- Significant-move notifications still use `SIGMA_IMPORTANT_THRESHOLD`.
- No sigma entry or exit policy was changed.
- No rejected candidate is released by this batch.
- No candidate rank score is changed by this batch.

## Why This Is Shadow-Only

The PC2 calibration table already shows historical context exists for VIX and sigma fields, but the authority status is not live-ready:

- VIX constants are classified as `delete_proof_required`.
- Sigma constants are classified as `context_measure_first`.

These constants influence higher-level routing and alert policy, not a simple candidate gate. Converting them directly into percentile thresholds would risk creating a new hidden hard regime rule instead of improving context awareness.

## Current Decision

Keep these constants as hard/static live behavior for now, but expose them in every snapshot so replay can prove:

- Whether VIX absolute thresholds are deleting or misrouting profitable families.
- Whether percentile VIX context should modify ranking rather than routing.
- Whether sigma thresholds belong in notification policy, entry scoring, or exit policy.

## Validation

Executed locally:

```text
python3 -m py_compile app/src/main/python/brain.py
python3 -m unittest app/src/main/python/tests/test_stage2a_guarded_ranking.py -k pc2
```

Result:

```text
Ran 3 tests in 0.002s
OK
```

## Recommended Next Batch

Proceed to cross-market input constants as evidence-only:

- `DOW_THRESHOLD`
- `CRUDE_THRESHOLD`
- `GIFT_THRESHOLD`

These currently have missing percentile history in the Batch 2 calibration table. The next step should first confirm whether the app stores reliable historical fields for these values before attempting live percentile use.
