# E1-B Context Scaling Report - 2026-07-23

## Scope Guard

- Offline only.
- No phone code changed.
- No live ranking authority.
- Test rows are primary-only in every arm.
- Context rows are strictly prior days.

## Dataset

- Rows: `8744`
- Role counts: `{'secondary': 8107, 'primary': 637}`
- Label counts: `{'0': 3672, '1': 5072}`
- Days: `27`
- Dataset SHA256: `a2aa514c7e43b459504cff5b9067548f13adc9a78f412f36cb500de1936f712c`

## Results

### A1_primary_25__logistic_baseline

- Status: `OK`
- Metrics: `{'rows': 426, 'auc': 0.5440560370624875, 'mean_within_day_auc': 0.5970977730323705, 'log_loss': 0.8394203633954828, 'brier': 0.2876764539114578, 'ece_10': 0.23850944366608182, 'median_batch_latency_sec': 0.1533302600000752, 'max_batch_latency_sec': 11.254038641996885, 'within_day_gradeable_days': 9, 'single_class_days': 8, 'first_context_rows': 53, 'last_context_rows': 159, 'max_context_rows': 159}`
- Day AUCs: `[{'day': '2026-06-23', 'auc': 0.9114583333333334, 'rows': 52, 'wins': 48}, {'day': '2026-06-24', 'auc': 0.8957528957528957, 'rows': 44, 'wins': 7}, {'day': '2026-06-29', 'auc': 0.15584415584415584, 'rows': 25, 'wins': 14}, {'day': '2026-07-01', 'auc': 0.5154061624649859, 'rows': 58, 'wins': 7}, {'day': '2026-07-02', 'auc': 0.6526315789473685, 'rows': 72, 'wins': 15}, {'day': '2026-07-03', 'auc': 0.4436936936936937, 'rows': 61, 'wins': 37}, {'day': '2026-07-07', 'auc': 0.99125, 'rows': 57, 'wins': 32}, {'day': '2026-07-08', 'auc': 0.1411764705882353, 'rows': 22, 'wins': 17}, {'day': '2026-07-09', 'auc': None, 'rows': 1, 'wins': 0}, {'day': '2026-07-13', 'auc': None, 'rows': 1, 'wins': 1}, {'day': '2026-07-14', 'auc': 0.6666666666666666, 'rows': 5, 'wins': 3}, {'day': '2026-07-16', 'auc': None, 'rows': 7, 'wins': 7}, {'day': '2026-07-17', 'auc': None, 'rows': 10, 'wins': 0}, {'day': '2026-07-20', 'auc': None, 'rows': 2, 'wins': 0}, {'day': '2026-07-21', 'auc': None, 'rows': 5, 'wins': 5}, {'day': '2026-07-22', 'auc': None, 'rows': 3, 'wins': 3}, {'day': '2026-07-23', 'auc': None, 'rows': 1, 'wins': 1}]`

### A2_primary_50__logistic_baseline

- Status: `OK`
- Metrics: `{'rows': 532, 'auc': 0.5643658301705092, 'mean_within_day_auc': 0.6561185708503819, 'log_loss': 1.0714569088328967, 'brier': 0.3059734677746726, 'ece_10': 0.25259870019783004, 'median_batch_latency_sec': 0.15165265649920912, 'max_batch_latency_sec': 0.37987156300005154, 'within_day_gradeable_days': 15, 'single_class_days': 9, 'first_context_rows': 52, 'last_context_rows': 318, 'max_context_rows': 318}`
- Day AUCs: `[{'day': '2026-06-05', 'auc': 0.7375565610859729, 'rows': 30, 'wins': 13}, {'day': '2026-06-08', 'auc': 0.26666666666666666, 'rows': 16, 'wins': 10}, {'day': '2026-06-09', 'auc': 1.0, 'rows': 16, 'wins': 7}, {'day': '2026-06-12', 'auc': 1.0, 'rows': 3, 'wins': 1}, {'day': '2026-06-15', 'auc': 0.13541666666666666, 'rows': 22, 'wins': 16}, {'day': '2026-06-17', 'auc': None, 'rows': 5, 'wins': 0}, {'day': '2026-06-22', 'auc': 0.25, 'rows': 14, 'wins': 10}, {'day': '2026-06-23', 'auc': 0.9114583333333334, 'rows': 52, 'wins': 48}, {'day': '2026-06-24', 'auc': 0.8996138996138996, 'rows': 44, 'wins': 7}, {'day': '2026-06-29', 'auc': 0.525974025974026, 'rows': 25, 'wins': 14}, {'day': '2026-07-01', 'auc': 0.7282913165266106, 'rows': 58, 'wins': 7}, {'day': '2026-07-02', 'auc': 0.5274853801169591, 'rows': 72, 'wins': 15}, {'day': '2026-07-03', 'auc': 0.6024774774774775, 'rows': 61, 'wins': 37}, {'day': '2026-07-07', 'auc': 0.98625, 'rows': 57, 'wins': 32}, {'day': '2026-07-08', 'auc': 0.27058823529411763, 'rows': 22, 'wins': 17}, {'day': '2026-07-09', 'auc': None, 'rows': 1, 'wins': 0}, {'day': '2026-07-13', 'auc': None, 'rows': 1, 'wins': 1}, {'day': '2026-07-14', 'auc': 1.0, 'rows': 5, 'wins': 3}, {'day': '2026-07-16', 'auc': None, 'rows': 7, 'wins': 7}, {'day': '2026-07-17', 'auc': None, 'rows': 10, 'wins': 0}, {'day': '2026-07-20', 'auc': None, 'rows': 2, 'wins': 0}, {'day': '2026-07-21', 'auc': None, 'rows': 5, 'wins': 5}, {'day': '2026-07-22', 'auc': None, 'rows': 3, 'wins': 3}, {'day': '2026-07-23', 'auc': None, 'rows': 1, 'wins': 1}]`

### A3_primary_100__logistic_baseline

- Status: `OK`
- Metrics: `{'rows': 574, 'auc': 0.5258893762183235, 'mean_within_day_auc': 0.6846375059485396, 'log_loss': 1.741059646164399, 'brier': 0.3395376447414478, 'ece_10': 0.30117664455020676, 'median_batch_latency_sec': 0.16454468749907392, 'max_batch_latency_sec': 0.21394010400035768, 'within_day_gradeable_days': 17, 'single_class_days': 9, 'first_context_rows': 63, 'last_context_rows': 636, 'max_context_rows': 636}`
- Day AUCs: `[{'day': '2026-06-03', 'auc': 0.3333333333333333, 'rows': 17, 'wins': 14}, {'day': '2026-06-04', 'auc': 0.6956521739130435, 'rows': 25, 'wins': 2}, {'day': '2026-06-05', 'auc': 0.7239819004524887, 'rows': 30, 'wins': 13}, {'day': '2026-06-08', 'auc': 0.3333333333333333, 'rows': 16, 'wins': 10}, {'day': '2026-06-09', 'auc': 0.9682539682539683, 'rows': 16, 'wins': 7}, {'day': '2026-06-12', 'auc': 1.0, 'rows': 3, 'wins': 1}, {'day': '2026-06-15', 'auc': 0.13541666666666666, 'rows': 22, 'wins': 16}, {'day': '2026-06-17', 'auc': None, 'rows': 5, 'wins': 0}, {'day': '2026-06-22', 'auc': 0.8, 'rows': 14, 'wins': 10}, {'day': '2026-06-23', 'auc': 0.9114583333333334, 'rows': 52, 'wins': 48}, {'day': '2026-06-24', 'auc': 0.9034749034749034, 'rows': 44, 'wins': 7}, {'day': '2026-06-29', 'auc': 0.525974025974026, 'rows': 25, 'wins': 14}, {'day': '2026-07-01', 'auc': 0.8011204481792717, 'rows': 58, 'wins': 7}, {'day': '2026-07-02', 'auc': 0.5684210526315789, 'rows': 72, 'wins': 15}, {'day': '2026-07-03', 'auc': 0.615990990990991, 'rows': 61, 'wins': 37}, {'day': '2026-07-07', 'auc': 0.98125, 'rows': 57, 'wins': 32}, {'day': '2026-07-08', 'auc': 0.3411764705882353, 'rows': 22, 'wins': 17}, {'day': '2026-07-09', 'auc': None, 'rows': 1, 'wins': 0}, {'day': '2026-07-13', 'auc': None, 'rows': 1, 'wins': 1}, {'day': '2026-07-14', 'auc': 1.0, 'rows': 5, 'wins': 3}, {'day': '2026-07-16', 'auc': None, 'rows': 7, 'wins': 7}, {'day': '2026-07-17', 'auc': None, 'rows': 10, 'wins': 0}, {'day': '2026-07-20', 'auc': None, 'rows': 2, 'wins': 0}, {'day': '2026-07-21', 'auc': None, 'rows': 5, 'wins': 5}, {'day': '2026-07-22', 'auc': None, 'rows': 3, 'wins': 3}, {'day': '2026-07-23', 'auc': None, 'rows': 1, 'wins': 1}]`

### A4_primary_secondary_100__logistic_baseline

- Status: `OK`
- Metrics: `{'rows': 574, 'auc': 0.4483674463937622, 'mean_within_day_auc': 0.5190325692277898, 'log_loss': 1.25936998915493, 'brier': 0.3401708288880767, 'ece_10': 0.22620485268710827, 'median_batch_latency_sec': 1.3329731244994036, 'max_batch_latency_sec': 2.1672521869986667, 'within_day_gradeable_days': 17, 'single_class_days': 9, 'first_context_rows': 620, 'last_context_rows': 8743, 'max_context_rows': 8743}`
- Day AUCs: `[{'day': '2026-06-03', 'auc': 1.0, 'rows': 17, 'wins': 14}, {'day': '2026-06-04', 'auc': 0.21739130434782608, 'rows': 25, 'wins': 2}, {'day': '2026-06-05', 'auc': 0.5701357466063348, 'rows': 30, 'wins': 13}, {'day': '2026-06-08', 'auc': 0.3333333333333333, 'rows': 16, 'wins': 10}, {'day': '2026-06-09', 'auc': 1.0, 'rows': 16, 'wins': 7}, {'day': '2026-06-12', 'auc': 1.0, 'rows': 3, 'wins': 1}, {'day': '2026-06-15', 'auc': 0.13541666666666666, 'rows': 22, 'wins': 16}, {'day': '2026-06-17', 'auc': None, 'rows': 5, 'wins': 0}, {'day': '2026-06-22', 'auc': 0.25, 'rows': 14, 'wins': 10}, {'day': '2026-06-23', 'auc': 0.9166666666666666, 'rows': 52, 'wins': 48}, {'day': '2026-06-24', 'auc': 0.8223938223938224, 'rows': 44, 'wins': 7}, {'day': '2026-06-29', 'auc': 0.5, 'rows': 25, 'wins': 14}, {'day': '2026-07-01', 'auc': 0.7478991596638656, 'rows': 58, 'wins': 7}, {'day': '2026-07-02', 'auc': 0.22573099415204678, 'rows': 72, 'wins': 15}, {'day': '2026-07-03', 'auc': 0.2894144144144144, 'rows': 61, 'wins': 37}, {'day': '2026-07-07', 'auc': 0.01125, 'rows': 57, 'wins': 32}, {'day': '2026-07-08', 'auc': 0.47058823529411764, 'rows': 22, 'wins': 17}, {'day': '2026-07-09', 'auc': None, 'rows': 1, 'wins': 0}, {'day': '2026-07-13', 'auc': None, 'rows': 1, 'wins': 1}, {'day': '2026-07-14', 'auc': 0.3333333333333333, 'rows': 5, 'wins': 3}, {'day': '2026-07-16', 'auc': None, 'rows': 7, 'wins': 7}, {'day': '2026-07-17', 'auc': None, 'rows': 10, 'wins': 0}, {'day': '2026-07-20', 'auc': None, 'rows': 2, 'wins': 0}, {'day': '2026-07-21', 'auc': None, 'rows': 5, 'wins': 5}, {'day': '2026-07-22', 'auc': None, 'rows': 3, 'wins': 3}, {'day': '2026-07-23', 'auc': None, 'rows': 1, 'wins': 1}]`

## Self-Audit

- TabPFN not included unless explicitly requested and token is available.
- TabFM ceiling not run.
- Secondary rows are lower-ranked candidates by construction; `training_role` and `role` are included as features.
- Large-context TabICL runtime is a measured deployment-ceiling signal, not hidden.
