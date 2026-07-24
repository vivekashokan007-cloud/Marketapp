# E1-D Retrieval Preservation Report - 2026-07-24

## Scope Guard

- Offline only.
- No phone code changed.
- No live ranking authority.
- Test rows are primary-only.
- Context rows are strictly prior-day.
- Retrieval distance excludes close-known/leaky fields.

## Dataset

- Rows: `8744`
- Role counts: `{'secondary': 8107, 'primary': 637}`
- Label counts: `{'0': 3672, '1': 5072}`
- Context size k: `256`
- Model: `tabicl`
- Dataset SHA256: `8aff063ff15897417e18524b5374f044d5a8dc53389f89b909b50c0954f13fb3`

## Retrieval Features

- Excluded close-known fields: `['bearish_close', 'bullish_close', 'day_direction', 'day_range', 'day_range_sigma', 'downtrend', 'inside_day', 'outside_day', 'uptrend']`
- Numeric retrieval features: `['buy_strike', 'consec_days', 'cost', 'dte', 'entry_credit', 'gap_sigma', 'max_loss', 'max_profit', 'move_sigma', 'sell_strike', 'sigma_away', 'spot', 'vix', 'weekday', 'width']`
- Categorical retrieval features: `['buy_strike2', 'day_group', 'day_vix', 'index', 'is_credit', 'mode', 'sell_strike2', 'strategy', 'vix_regime']`

## Results

### stratified_256

- Status: `OK`
- Metrics: `{'rows': 574, 'auc': 0.5272051656920078, 'mean_within_day_auc': 0.5887540153483535, 'log_loss': 0.856796846996478, 'brier': 0.3009139633322096, 'ece_10': 0.248736574454882, 'median_batch_latency_sec': 1.9669235149995075, 'max_batch_latency_sec': 34.66892738200113, 'within_day_gradeable_days': 17, 'single_class_days': 9, 'mean_context_win_rate': 0.5286571755226481, 'mean_primary_context_rows': 23.48780487804878, 'mean_secondary_context_rows': 232.5121951219512}`
- Day AUCs: `[{'day': '2026-06-03', 'auc': 1.0, 'rows': 17, 'wins': 14}, {'day': '2026-06-04', 'auc': 0.6956521739130435, 'rows': 25, 'wins': 2}, {'day': '2026-06-05', 'auc': 0.8235294117647058, 'rows': 30, 'wins': 13}, {'day': '2026-06-08', 'auc': 0.3333333333333333, 'rows': 16, 'wins': 10}, {'day': '2026-06-09', 'auc': 0.6507936507936508, 'rows': 16, 'wins': 7}, {'day': '2026-06-12', 'auc': 1.0, 'rows': 3, 'wins': 1}, {'day': '2026-06-15', 'auc': 0.0, 'rows': 22, 'wins': 16}, {'day': '2026-06-17', 'auc': None, 'rows': 5, 'wins': 0}, {'day': '2026-06-22', 'auc': 0.75, 'rows': 14, 'wins': 10}, {'day': '2026-06-23', 'auc': 0.46875, 'rows': 52, 'wins': 48}, {'day': '2026-06-24', 'auc': 0.4671814671814672, 'rows': 44, 'wins': 7}, {'day': '2026-06-29', 'auc': 0.7402597402597403, 'rows': 25, 'wins': 14}, {'day': '2026-07-01', 'auc': 0.23249299719887956, 'rows': 58, 'wins': 7}, {'day': '2026-07-02', 'auc': 0.5461988304093567, 'rows': 72, 'wins': 15}, {'day': '2026-07-03', 'auc': 0.509009009009009, 'rows': 61, 'wins': 37}, {'day': '2026-07-07', 'auc': 0.9975, 'rows': 57, 'wins': 32}, {'day': '2026-07-08', 'auc': 0.29411764705882354, 'rows': 22, 'wins': 17}, {'day': '2026-07-09', 'auc': None, 'rows': 1, 'wins': 0}, {'day': '2026-07-13', 'auc': None, 'rows': 1, 'wins': 1}, {'day': '2026-07-14', 'auc': 0.5, 'rows': 5, 'wins': 3}, {'day': '2026-07-16', 'auc': None, 'rows': 7, 'wins': 7}, {'day': '2026-07-17', 'auc': None, 'rows': 10, 'wins': 0}, {'day': '2026-07-20', 'auc': None, 'rows': 2, 'wins': 0}, {'day': '2026-07-21', 'auc': None, 'rows': 5, 'wins': 5}, {'day': '2026-07-22', 'auc': None, 'rows': 3, 'wins': 3}, {'day': '2026-07-23', 'auc': None, 'rows': 1, 'wins': 1}]`

## Self-Audit

- Full all-role context is not rerun here because E1B A4 TabICL timed out at 600s.
- k-NN context is query-specific, so runtime is per candidate, not per poll batch.
- recent/random contexts are per-day contexts and can score a day batch together.
- Retrieval feature safety is conservative but not a formal market-data availability proof.
