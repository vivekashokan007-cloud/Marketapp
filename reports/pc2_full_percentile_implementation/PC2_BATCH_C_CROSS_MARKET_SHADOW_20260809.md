# PC2 Batch C - Cross-Market Threshold Shadow

Date: 2026-08-09

## Scope

This batch records the Dow, Crude, and GIFT threshold constants in the live snapshot artifact without changing live brain behavior.

No Supabase calls were made.

## Constants Covered

| Constant | Current value | Runtime field | Current role | Batch C authority | Live softened |
| --- | ---: | --- | --- | --- | --- |
| DOW_THRESHOLD | 0.5 | Dow percentage move | Global direction alignment/conflict | missing_percentile_history | No |
| CRUDE_THRESHOLD | 1.5 | Crude percentage move | Global direction alignment/conflict | missing_percentile_history | No |
| GIFT_THRESHOLD | 0.3 | GIFT percentage move | Global direction alignment/conflict | missing_percentile_history | No |

## Finding

The app computes these signals in runtime logic:

- Dow move from `dowClose` to `dowNow`
- Crude move from `crudeSettle` to `crudeNow`
- GIFT move from evening close to current GIFT

But the existing PC2 calibration artifact has zero durable percentile support for:

- `dow_pct_change`
- `crude_pct_change`
- `gift_pct_change`

Therefore, these constants cannot be converted into percentile authority yet.

## What Changed

Added snapshot-only inventory:

- `PC2_BATCH_C_CROSS_MARKET_VERSION = pc2_batch_c_cross_market_shadow_v1`
- `_pc2_batch_c_cross_market_inventory()`
- `market_forces.pc2_batch_c_cross_market`
- `poll_summary.pc2_batch_c_cross_market_status`
- `poll_summary.pc2_batch_c_cross_market_shadow_count`
- `poll_summary.pc2_batch_c_cross_market_live_softened_count`
- `snapshot_context.snapshot_pc2_batch_c_cross_market`

## What Did Not Change

- Global direction conflict scoring remains unchanged.
- Dow, Crude, and GIFT thresholds remain absolute constants.
- No candidate generation, rejection, ranking, notification, or exit behavior changed.

## Practical Conclusion

This group is not ready for live percentile use. The correct next engineering step is to add or verify durable storage of the computed percentage moves, then backfill them into the percentile history population. Only after that should percentile replay decide whether these constants become soft context, ranking features, or remain fixed guardrails.

## Validation

Executed locally:

```text
python3 -m py_compile app/src/main/python/brain.py
python3 -m unittest app/src/main/python/tests/test_stage2a_guarded_ranking.py -k pc2
```

Result:

```text
Ran 4 tests in 0.003s
OK
```

## Recommended Next Batch

Proceed to G2 exit policy constants as mechanism-only evidence:

- `TARGET_NEAR_RATIO`
- `STOP_LOSS_RATIO`

These should not become percentile ranking gates. They belong to position management and need exit-path parity evidence.
