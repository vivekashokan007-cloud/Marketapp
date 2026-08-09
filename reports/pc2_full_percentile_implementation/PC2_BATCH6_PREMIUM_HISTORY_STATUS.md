# PC2 Batch 6 - Premium History Backfill Status

## Scope

Batch 6 checks the Tier-1 `premium_history` / daily-history support layer needed before live percentile authority.

No runtime app behavior was changed in this batch.

## Actions Run

```bash
python3 tools/b1_percentile_backfill_controller.py --stage schema_probe --page-size 3 --max-pages 1 --sleep-sec 1.5 --timeout 120
python3 tools/b1_percentile_backfill_controller.py --stage tier1_premium_history --insert-missing-only --page-size 50 --max-pages 20 --write-chunk 25 --sleep-sec 1.5 --timeout 180
python3 tools/b1_percentile_backfill_controller.py --stage tier1_premium_history --page-size 50 --max-pages 20 --write-chunk 25 --sleep-sec 1.5 --timeout 180
```

## Result

- Supabase config: available.
- Remote read probe:
  - `premium_history`: OK.
  - `ml_context_percentile_history`: OK.
- Local migration permits `history_source` values:
  - `live`
  - `backfill`
- `backfill_premium_history` and `backfill_replay` are not currently allowed source names; use `history_source='backfill'` with `source_table='premium_history'`.

## Missing-Only Probe

- `premium_history` source rows read: `146`.
- Existing `ml_context_percentile_history` rows with `source_table='premium_history'` read: `260`.
- Candidate rows after missing-only filter: `0`.
- Supabase writes performed: `0`.

Interpretation: current premium-history backfill rows are already present remotely. Rewriting them would only add load.

## Full Local Artifact Regenerated

- `premium_history` source rows read: `146`.
- Tier-1 candidate rows built locally: `237`.
- Supabase writes performed: `0`.
- Local CSV regenerated:
  - `reports/b1_percentile_backfill_20260805/tier1_premium_history_rows.csv`
- Local report regenerated:
  - `reports/b1_percentile_backfill_20260805/B1_TIER1_PREMIUM_HISTORY_DRY_RUN.md`

## Coverage Notes

- `vix` has useful 60-window support from premium history:
  - rows: `127`
  - non-null: `127`
  - latest support_60: `60`
- Institutional fields in raw `premium_history` remain sparse:
  - `fii_short_pct`: `19`
  - `fii_cash`: `19`
  - `dii_cash`: `16`
  - `fii_idx_fut`: `15`
  - `fii_stk_fut`: `16`
  - `bias_net`: `25`
- Snapshot-derived incremental rows for `2026-08-06` and `2026-08-07` already exist locally and were previously written according to their reports.

## Decision

Batch 6 premium-history support is complete enough for the next PC2 step:

- no Supabase write required today
- no throttling-heavy operation required
- proceed to Batch 5 live gate-basis wiring only with explicit fallback and attribution intact

