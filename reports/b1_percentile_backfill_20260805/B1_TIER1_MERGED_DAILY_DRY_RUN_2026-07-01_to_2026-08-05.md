# B1 Tier 1 Merged Daily Percentile Backfill Dry Run

- Generated at UTC: `2026-08-05T14:47:51Z`.
- Premium source rows read: `146`.
- Snapshot source rows read: `1959`.
- Snapshot start: `2026-07-01`.
- Snapshot end: `2026-08-05`.
- Merged trading days: `153`.
- Candidate percentile rows built: `416`.
- Supabase rows written: `0` dry-run.
- Source day counts: `{'premium_history': 127, 'ml_brain_snapshots': 26}`.
- CSV: `/root/Documents/Codex/2026-07-04/this-my-project-read-and-understand/Marketapp-main-worktree/reports/b1_percentile_backfill_20260805/tier1_merged_daily_rows_2026-07-01_to_2026-08-05.csv`.

## Variable Coverage
- `vix`: rows `153`, non-null `153`, pct_60 rows `152`, support_count_60>=60 `93`, latest support_60 `60` latest pct_60 `10.0`, latest non-null `2026-08-05` value `12.09`
- `fii_short_pct`: rows `50`, non-null `50`, pct_60 rows `49`, support_count_60>=60 `0`, latest support_60 `49` latest pct_60 `50.0`, latest non-null `2026-08-05` value `87.0`
- `fii_cash`: rows `50`, non-null `50`, pct_60 rows `49`, support_count_60>=60 `0`, latest support_60 `49` latest pct_60 `93.88`, latest non-null `2026-08-05` value `2446.5`
- `dii_cash`: rows `46`, non-null `46`, pct_60 rows `45`, support_count_60>=60 `0`, latest support_60 `45` latest pct_60 `8.89`, latest non-null `2026-08-05` value `-936.1`
- `fii_idx_fut`: rows `45`, non-null `45`, pct_60 rows `44`, support_count_60>=60 `0`, latest support_60 `44` latest pct_60 `34.09`, latest non-null `2026-08-05` value `-500.0`
- `fii_stk_fut`: rows `46`, non-null `46`, pct_60 rows `45`, support_count_60>=60 `0`, latest support_60 `45` latest pct_60 `15.56`, latest non-null `2026-08-05` value `-1170.0`
- `bias_net`: rows `26`, non-null `26`, pct_60 rows `25`, support_count_60>=60 `0`, latest support_60 `25` latest pct_60 `64.0`, latest non-null `2026-06-29` value `1.0`

## Interpretation

- Signed FII/DII values are extracted from both daily history and live snapshot context.
- VIX snapshot rows are collapsed using last poll of session.
- Manual institutional snapshot rows are collapsed using modal value, latest as tie-breaker.
- PCR excluded from merged backfill: premium_history PCR and chain PCR are definitionally incomparable.
- `source_table` is actual per row (`premium_history` or `ml_brain_snapshots`), never a concatenated label.
- `source_quality` marks splice windows when prior percentile support spans both sources.
- This avoids declaring user-entered data missing when it is stored in snapshots rather than `premium_history`.
- Live app reads must prefer `history_source=live` over `backfill` on same session/variable; app-side precedence has been added separately.

Final status: `WARN`.
