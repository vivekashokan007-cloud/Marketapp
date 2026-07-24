# Chapter B Start Audit - 2026-07-22

## Status

- Chapter A was closed by Claude in `CLAUDE_RULING_A1_CHAPTER_A_CLOSED_20260722.md`.
- Authorized next phase: Chapter B evidence engine.
- Authorized tracks:
  - Class B parity gate, Tier 1, five reference-day harness proof.
  - E1 in-context tabular model bake-off, offline only.

## Work Performed

- Inspected existing Chapter B harness entry points in `historical_replay_harness.py`:
  - `--class-b-audit`
  - `--class-b-extract-day`
  - `--class-b-local-day`
  - `--class-b-parity-day`
- Patched the Class B audit probes to avoid unbounded `historical_option_candles` span/sample scans.
- Verified Python syntax with:
  - `python3 -m py_compile historical_replay_harness.py`

## Supabase Safety Patch

The original Class B audit called `_fetch_historical_option_span()` without a date predicate. Supabase timed out on the global ordered span query:

- table: `historical_option_candles`
- failure: statement timeout
- risk: expensive full-table ordered scan

The audit now passes `date_from` and `date_to` into:

- `_fetch_historical_option_sample(...)`
- `_fetch_historical_option_span(...)`

Both helpers add a bounded `bar_ts` window derived from the requested NSE session-date range.

## Bounded Audit Attempt

Command shape:

```bash
python3 historical_replay_harness.py \
  --class-b-audit \
  --from 2026-07-07 \
  --to 2026-07-22 \
  --sample-days 10
```

Result:

- The original global timeout was removed.
- Supabase then returned Cloudflare origin errors during snapshot/day probes:
  - HTTP `525` SSL handshake failed
  - HTTP `521` origin down
- Per throttling discipline, no aggressive retry loop was run.
- Audit did not reach Class B parity-ready state.

Observed partial output before stopping:

- `historical_option_candles_sample_rows = 0`
- bounded span returned no first/last bar before origin failures.
- `daily_data_probe` sampled zero rows for the requested window.
- saved snapshot dates partially fetched before origin failures:
  - `2026-07-07`: 75 snapshots, 74 Class A, 510 generated candidates.
  - `2026-07-08`: 76 snapshots, 75 Class A, 99 generated candidates.

Current Class B status:

- `BLOCKED_BY_SUPABASE_ORIGIN`
- Not a harness proof failure.
- Not a brain logic failure.
- Retry should wait for Supabase stability and use one day at a time.

## One-Day Micro-Probe Follow-Up

After the bounded audit was patched, a one-day-only check was run for `2026-07-07` with the standing slow-read discipline.

One-day audit result:

- `2026-07-07`: 75 snapshots, 74 Class A, 510 generated candidates.
- `historical_option_candles_sample_rows = 0`
- `historical_option_candles_span first = -- last = --`
- `daily_data_probe.rows_sampled = 0`
- audit blocked with:
  - `historical_option_candles_missing_required_columns`

That block reason is a side effect of zero sampled rows. It does **not** prove the table lacks those columns globally.

Direct REST micro-probes then narrowed the cause further:

- `historical_option_candles` global existence probe:
  - `HTTP 200`
  - returned one real row:
    - `bar_ts=2024-09-26T03:45:00+00:00`
    - `underlying=NF`
    - `expiry_date=2024-10-03`
    - `strike_price=24250.0`
    - `option_type=PE`
    - `close=3.3`
    - `open_interest=32525`
- bounded `historical_option_candles` probe for `2026-07-07`:
  - `HTTP 200`
  - returned `[]`
- `daily_data` probe for `2026-07-07`:
  - `HTTP 200`
  - returned `[]`

Interpretation:

- This is no longer a live Supabase outage for the one-day path.
- This is not a Class B harness defect.
- The immediate blocker is data coverage for the chosen reference day:
  - `historical_option_candles` exists globally but has no rows for `2026-07-07`.
  - `daily_data` also has no rows for `2026-07-07`.
- Therefore `2026-07-07` cannot serve as the first Class B parity reference day through the current data path.

## Extended Micro-Probe Window

To avoid guessing about a single bad day, the same direct REST micro-probes were repeated for:

- `2026-07-08`
- `2026-07-09`
- `2026-07-10`

Results:

- `daily_data`
  - `2026-07-08`: `HTTP 200`, `[]`
  - `2026-07-09`: `HTTP 200`, `[]`
  - `2026-07-10`: `HTTP 200`, `[]`
- `historical_option_candles`
  - `2026-07-08`: `HTTP 200`, `[]`
  - `2026-07-09`: `HTTP 200`, `[]`
  - `2026-07-10`: `HTTP 200`, `[]`

Refined conclusion:

- For the tested Chapter B reference window `2026-07-07` through `2026-07-10`, the historical backing tables are empty through the current Supabase path.
- That means the immediate problem is broader than "pick a different first day" inside this window.
- Before Class B parity can proceed on July 2026 days, we need either:
  - the actual July 2026 historical tables populated, or
  - the correct alternative source/table/path identified if the harness is pointed at the wrong historical dataset.

## Owed Debt: 629 -> 620 Replay-Row Accounting

The apparent `629 -> 620` discrepancy is explained by the original D3 single-day row accounting:

- Source report:
  - `reports/d3_blocked_candidate_replay_report.md`
- Source rows:
  - `reports/d3_blocked_candidate_replay_rows.csv`
- Total replay rows:
  - `646`
- Anchor split:
  - `teacher_eval_match = 26`
  - `simulated_trace = 620`
  - `recommendation_match = 0`
  - `pricing_failed = 0`

Interpretation:

- `620` is not the full replay row count.
- `620` is the simulated-trace subset after 26 teacher-matched rows are separated.
- The total replay population for that single-day artifact is `646`.

For the wider D3A report:

- Source rows:
  - `reports/d3a_full_replay_rows_20260707_20260721.csv`
- Total rows:
  - `10676`
- Anchor split:
  - `teacher_eval_match = 831`
  - `simulated_trace = 9378`
  - `pricing_failed = 467`
  - `recommendation_match = 0`

## Owed Debt: D5 `a8_bypassed` Count

- Current retained D3/D3A CSV artifacts do not contain an `a8_bypassed` column.
- The current available cohort fields are:
  - `A8_SURVIVOR`
  - `A8_KILLED`
- Current D3A cohort counts:
  - `A8_SURVIVOR = 832`
  - `A8_KILLED = 9844`
- Therefore, `a8_bypassed` cannot be reconstructed from the retained D3A CSV alone.

Required follow-up:

- Either locate the original D5 artifact containing `a8_bypassed`, or rerun the relevant D5 report generator if it still exists.
- Do not infer `a8_bypassed` from `A8_SURVIVOR` or `A8_KILLED`; that would fabricate a metric.

## Next Safe Step

When Supabase is stable:

1. Run `--class-b-audit` over a one-day window first, not the full date range.
2. Pick one Class A day with historical candles.
3. Run `--class-b-extract-day` for that day.
4. Run `--class-b-local-day` from the local extract.
5. Only after one-day local parity is understood, expand to five reference days.

No live phone code, ranking code, sandbox authority, or A8 floor behavior was changed by this audit.
