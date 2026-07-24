# E1-E1 P&L Ranking Report - 2026-07-24

## Scope Guard

- Offline only.
- No phone code changed.
- No live ranking authority.
- Target is friction-true P&L, not win/loss.
- Full generated menus are scored, with synthetic `NO_TRADE` row added.

## Dataset

- Rows: `8744`
- Snapshots with one primary: `637`
- Scored menus: `574`
- Target column: `net_pnl`
- Context: `random_256`, k `256`
- Dataset SHA256: `8aff063ff15897417e18524b5374f044d5a8dc53389f89b909b50c0954f13fb3`

## Metrics

- Metrics: `{'menus': 574, 'candidate_predictions': 5205, 'model_total_pnl': -35987.0, 'brain_total_pnl': -18980.25, 'random_total_pnl': -28832.092743041274, 'oracle_total_pnl': 369436.75, 'model_avg_pnl': -62.69512195121951, 'brain_avg_pnl': -33.06663763066202, 'random_avg_pnl': -50.230126730037064, 'oracle_avg_pnl': 643.618031358885, 'model_minus_brain_avg': -29.62848432055749, 'model_minus_random_avg': -12.464995221182448, 'brain_minus_random_avg': 17.16348909937504, 'oracle_gap_capture_model_pct': -1.7965018824168966, 'oracle_gap_capture_brain_pct': 2.4736664498250938, 'no_trade_picks': 123, 'no_trade_pick_rate': 0.21428571428571427, 'candidate_spearman_pred_actual': 0.07692844072017356, 'median_batch_latency_sec': 16.78234650399827, 'max_batch_latency_sec': 40.71646821399918}`

## Interpretation

- `model_avg_pnl` is the realised P&L of the TabICL top-scored candidate, including `NO_TRADE` if selected.
- `brain_avg_pnl` is the realised P&L of the existing primary candidate.
- `random_avg_pnl` is the menu-average realised P&L including `NO_TRADE`.
- `oracle_avg_pnl` is the best realised P&L in the menu including `NO_TRADE`.

## Self-Audit

- This is one context strategy only: deterministic `random_256`, selected because E1-D made it the best completed full-run arm.
- Regression target scale is raw rupees; no clipping or normalization beyond TabICL internals.
- This measures selection from already generated/evaluated menus, not candidate generation.
- NO_TRADE is synthetic and has realised P&L exactly zero.
