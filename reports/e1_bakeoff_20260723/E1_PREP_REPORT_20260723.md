# E1 Prep Report - 2026-07-23

## Scope

- Offline E1 prep only.
- No phone code.
- No live ranking authority.
- Source table: `ml_evaluation_outcomes_s1`.
- Filter: `role=primary`, `new_price_integrity=OK`, `new_canonical_won not null`.

## Dataset

- Rows: `637`
- Source S1 rows: `637`
- Distinct days: `27`
- Feature columns: `50`
- Dataset SHA256: `e6b52e2984c8f9362b28ae5bd3c95599eb6994602884766ce4483be2445f81e1`
- CSV: `/root/Documents/Codex/2026-07-04/this-my-project-read-and-understand/Marketapp-main-worktree/reports/e1_bakeoff_20260723/e1_primary_dataset.csv`
- JSONL: `/root/Documents/Codex/2026-07-04/this-my-project-read-and-understand/Marketapp-main-worktree/reports/e1_bakeoff_20260723/e1_primary_dataset.jsonl`
- Skipped: `{}`

## Label Base

- Wins: `328`
- Losses: `309`
- Win rate: `51.49%`

## Days

- `2026-06-02`: rows `63`, wins `58`, win_rate `92.06`
- `2026-06-03`: rows `17`, wins `14`, win_rate `82.35`
- `2026-06-04`: rows `25`, wins `2`, win_rate `8.0`
- `2026-06-05`: rows `30`, wins `13`, win_rate `43.33`
- `2026-06-08`: rows `16`, wins `10`, win_rate `62.5`
- `2026-06-09`: rows `16`, wins `7`, win_rate `43.75`
- `2026-06-12`: rows `3`, wins `1`, win_rate `33.33`
- `2026-06-15`: rows `22`, wins `16`, win_rate `72.73`
- `2026-06-17`: rows `5`, wins `0`, win_rate `0.0`
- `2026-06-22`: rows `14`, wins `10`, win_rate `71.43`
- `2026-06-23`: rows `52`, wins `48`, win_rate `92.31`
- `2026-06-24`: rows `44`, wins `7`, win_rate `15.91`
- `2026-06-29`: rows `25`, wins `14`, win_rate `56.0`
- `2026-07-01`: rows `58`, wins `7`, win_rate `12.07`
- `2026-07-02`: rows `72`, wins `15`, win_rate `20.83`
- `2026-07-03`: rows `61`, wins `37`, win_rate `60.66`
- `2026-07-07`: rows `57`, wins `32`, win_rate `56.14`
- `2026-07-08`: rows `22`, wins `17`, win_rate `77.27`
- `2026-07-09`: rows `1`, wins `0`, win_rate `0.0`
- `2026-07-13`: rows `1`, wins `1`, win_rate `100.0`
- `2026-07-14`: rows `5`, wins `3`, win_rate `60.0`
- `2026-07-16`: rows `7`, wins `7`, win_rate `100.0`
- `2026-07-17`: rows `10`, wins `0`, win_rate `0.0`
- `2026-07-20`: rows `2`, wins `0`, win_rate `0.0`
- `2026-07-21`: rows `5`, wins `5`, win_rate `100.0`
- `2026-07-22`: rows `3`, wins `3`, win_rate `100.0`
- `2026-07-23`: rows `1`, wins `1`, win_rate `100.0`

## Baselines

- Walk-forward base-rate: `{'auc': 0.45387426900584793, 'log_loss': 0.7905945730708254, 'brier': 0.2879669666501715, 'coverage_rows': 574}`
- Frozen model score availability: `{'auc': None, 'log_loss': None, 'brier': None, 'coverage_rows': 0}`

## Package Probe

- `tabicl`: available `False`, origin `None`
- `tabpfn`: available `False`, origin `None`
- `torch`: available `False`, origin `None`
- `sklearn`: available `False`, origin `None`
- `numpy`: available `False`, origin `None`
- `pandas`: available `False`, origin `None`

## Self-Audit - Deviations From Pre-Registration

- TabICL/TabPFN model runs were not started in this prep step.
- TabFM ceiling run was not started.
- This step freezes the dataset and baselines only.
- If a package is unavailable locally, installation/runtime feasibility must be handled as the next E1 step before any result claim.
