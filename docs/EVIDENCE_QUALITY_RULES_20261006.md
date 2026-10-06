# Research evidence-quality rules v1

Written 6 October 2026. This page is research-only: no production row is changed or deleted. The machine-readable copy is `research/evidence_quality_rules_v1.json`.

| Rule id | Table / field | Window | Rule | Evidence |
| --- | --- | --- | --- | --- |
| `EQ1_vix_direction_invalid` | `trades_v2.entry_snapshot.vix_direction` | entries 2026-07-02 → release of 2.6.66 on the phone | Exclude: the value equals `entry_vix − 13.61` (the 29 June morning VIX). | Verified on all 136 trades, 2 Jul–5 Oct, within 0.01 (5 Oct audit). |
| `EQ2_fii_deriv_net_missing_as_zero` | `trades_v2.entry_snapshot.fii_deriv_net` | entries 2026-07-02 → release of 2.6.66 | Treat `0` as **missing**. Outside the window a recorded 0 is **unverified**, not proven valid: no row-level provenance shows it was measured, so do not admit it to training on this rule alone (v2 counts it as `outside_window_zero_unverified`). | 0 on all 136 trades, 2 Jul–5 Oct (the 5 Oct audit verified this window; a wider run from 19 May is reported but not independently verified). |
| `EQ3_fii_short_vote_missing_history` | `morningBias.signals[name='FII Short%']` | all sessions until the Paper abstention patch is live | Votes with value text containing `prev: N/A` are **level-only fallbacks**, not change-based votes. Exclude them from any FII-skill analysis. | The 40 stored days, 6 Aug–5 Oct, were all BEAR. |
| `EQ4_chain_snapshots_frozen` | `chain_snapshots` | after 2026-06-29 | No valid rows (writer rejected with 400). Do not use. Retired in 2.6.66. | Edge logs, 5 Oct: 16 × POST 400. |
| `EQ5_premium_history_frozen` | `premium_history` / ctx `premiumHistory`, `yesterdayHistory`, `fiiHistory` | after 2026-06-29 | Absent, not stale-valid. | Last row 2026-06-29. |
| `EQ6_option_chain_partial_capture` | `ml_option_chain_snapshots` | before 2026-06-22 | About 40 rows/poll and blank `expiry` rows. Full-chain research starts 2026-06-22. Rows with a non-date `expiry` are invalid. | Weekly row counts. |
| `EQ7_partial_sessions` | `ml_option_chain_snapshots` | 2026-09-28 (9 polls, last 14:50); 2026-07-27 (last poll 15:00) | No scheduled 15:20 close. An outcome needing that close is **unknown**, never filled from an earlier poll. | 5 Oct audit, 6 Oct extract. |
| `EQ8_paper_flag_semantics` | `trades_v2.paper` | 2026-03-20 → 25 | `paper=false` rows carry `execution_mode='paper'`. There is no verified live-money record. | 5 Oct audit. |
| `EQ9_teacher_same_session_only` | `ml_evaluation_outcomes` | all | Labels are same-session managed outcomes (see the terminology page). Do not use them as overnight evidence. | All 3,330 recent rows exit on the entry session. |
| `EQ10_device_version_mixing` | `ml_brain_snapshots.brain_version` | any session | A session with mixed or partial versions is not a homogeneous full-session result (DB-1 and research). | Review gate 6 Oct. |

Original rows are always preserved. Exclusions are applied by research readers through these rule ids, and every exclusion is counted in published denominators.
