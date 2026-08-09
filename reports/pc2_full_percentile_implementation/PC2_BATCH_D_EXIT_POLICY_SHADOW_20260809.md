# PC2 Batch D - G2 Exit Policy Shadow

Date: 2026-08-09

## Scope

This batch records the G2 exit-policy constants in the live snapshot artifact without changing entry ranking, candidate generation, notifications, or position-management behavior.

No Supabase calls were made.

## Constants Covered

| Constant | Current value | Runtime role | Batch D authority | Live softened |
| --- | ---: | --- | --- | --- |
| TARGET_NEAR_RATIO | 0.8 | Position target-near alert threshold | g2_mechanism_only | No |
| STOP_LOSS_RATIO | 0.7 | Position stop-loss alert threshold | g2_mechanism_only | No |

## Finding

These constants are not entry-selection gates. They operate after a position exists:

- `TARGET_NEAR_RATIO` fires a target-near/book-profit alert when current P&L reaches the configured percentage of max profit.
- `STOP_LOSS_RATIO` fires a stop-loss alert when current P&L reaches the configured percentage of max loss.

Converting these into entry-ranking percentiles would be conceptually wrong. They belong to exit-path parity and live position management.

## What Changed

Added snapshot-only inventory:

- `PC2_BATCH_D_EXIT_POLICY_VERSION = pc2_batch_d_exit_policy_shadow_v1`
- `_pc2_batch_d_exit_policy_inventory()`
- `market_forces.pc2_batch_d_exit_policy`
- `poll_summary.pc2_batch_d_exit_policy_status`
- `poll_summary.pc2_batch_d_exit_policy_shadow_count`
- `poll_summary.pc2_batch_d_exit_policy_live_softened_count`
- `snapshot_context.snapshot_pc2_batch_d_exit_policy`

## What Did Not Change

- No target-near alert threshold changed.
- No stop-loss alert threshold changed.
- No entry candidate ranking changed.
- No rejected candidate is released.
- No live trade action changes.

## Practical Conclusion

These constants should remain under G2 mechanism governance until exit-path parity is proven. If they are later adapted, the adaptation should be tied to realized managed-exit performance, not to the entry candidate percentile framework.

## Validation

Executed locally:

```text
python3 -m py_compile app/src/main/python/brain.py
python3 -m unittest app/src/main/python/tests/test_stage2a_guarded_ranking.py -k pc2
```

Result:

```text
Ran 5 tests in 0.000s
OK
```

## Recommended Next Work

The remaining PC2 groups need data design before live conversion:

- Candle pattern thresholds need candle-body/shadow history.
- Microstructure constants need time-of-day and liquidity history.
- Cross-market constants need durable Dow/Crude/GIFT percentage history.

Do not convert these into live percentile rules until the support population exists.
